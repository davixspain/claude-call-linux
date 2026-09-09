"""claude-call doctor — checks your setup and benchmarks the voice stack.

Run:  claude-call doctor
Verifies prerequisites, model, config sanity, and measures STT + TTS latency so
you know the call will be fast. Exit code 1 if anything is broken.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; D = "\033[2m"; X = "\033[0m"
R_ = {"fail": 0, "warn": 0}


def ok(m):   print(f"  {G}✓{X} {m}")
def warn(m): print(f"  {Y}!{X} {m}"); R_["warn"] += 1
def bad(m):  print(f"  {R}✗{X} {m}"); R_["fail"] += 1
def dim(m):  print(f"  {D}· {m}{X}")
def head(m): print(f"\n\033[1m{m}\033[0m")


def need(binary, label, hard=True):
    if shutil.which(binary):
        ok(f"{label}  ({binary})")
        return True
    (bad if hard else warn)(f"{label} not found: {binary}")
    return False


async def main():
    import config as C

    head("Prerequisites")
    v = sys.version_info
    if (v.major, v.minor) in [(3, 11), (3, 12)]:
        ok(f"Python {v.major}.{v.minor}")
    else:
        warn(f"Python {v.major}.{v.minor} (use 3.12; 3.13 removed audioop)")
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop  # noqa: F401
        ok("audioop available")
    except Exception:
        bad("audioop missing (Python 3.13?) — STT resampling will break")
    need("claude", "Claude Code (the brain)")
    dim("(make sure you've run `claude` once and logged in)")
    need("uv", "uv")
    need("ffmpeg", "ffmpeg")
    has_server = need("whisper-server", "whisper-server (fast STT)", hard=False)
    has_cli = bool(shutil.which("whisper-cli"))
    if not has_server and not has_cli:
        bad("no whisper.cpp — install it (whisper-server or whisper-cli)")
    elif has_cli and not has_server:
        ok("whisper-cli (fallback)")
    if shutil.which("swiftc"):
        ok("swiftc — macOS AEC available")
    else:
        dim("swiftc not found — macOS AEC unavailable (optional)")

    head("Model & config")
    model = Path(C.WHISPER_MODEL)
    if model.exists():
        ok(f"whisper model: {model.name} ({model.stat().st_size // 1_000_000} MB)")
    else:
        bad(f"model not found: {model}  → run scripts/download-model.sh")
    if C.SAMPLE_RATE_IN == 16000:
        ok("input 16 kHz (Silero VAD + whisper)")
    else:
        bad(f"input {C.SAMPLE_RATE_IN} Hz — Silero needs 16000!")
    ok(f"output {C.SAMPLE_RATE_OUT // 1000} kHz (TTS)")

    from tts import PROVIDERS, _PREMIUM_DEFAULTS
    if C.TTS not in PROVIDERS:
        bad(f"unknown CALL_TTS={C.TTS} (options: {', '.join(PROVIDERS)})")
    elif C.TTS == "edge":
        ok(f"voice: edge / {C.VOICE} (free)")
    else:
        env_key = _PREMIUM_DEFAULTS[C.TTS][2]
        key = C.TTS_API_KEY or os.getenv(env_key)
        if key:
            ok(f"voice: {C.TTS} (premium, key set, voice={C.VOICE or 'default'})")
        else:
            warn(f"voice: {C.TTS} but NO key → it will fall back to free edge")

    try:
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        SileroVADAnalyzer()
        ok("Silero VAD loads")
    except Exception as e:  # noqa: BLE001
        bad(f"Silero VAD failed: {e}")

    head("Microphone")
    # A falha nº 1 de campo é permissão de mic negada / device errado — testa DE VERDADE:
    # abre o mic ~0.7s e mede o pico. (No macOS isso também dispara o prompt de permissão
    # já no doctor, em vez de na primeira call.)
    try:
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore", DeprecationWarning)
            import audioop as _audioop
        import pyaudio
        pa = pyaudio.PyAudio()
        st = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                     frames_per_buffer=1024)
        peak = 0
        for _ in range(int(16000 / 1024 * 0.7)):
            peak = max(peak, _audioop.rms(st.read(1024, exception_on_overflow=False), 2))
        st.stop_stream(); st.close(); pa.terminate()
        if peak > 30:
            ok(f"mic capturing (peak RMS {peak})")
        else:
            warn("mic opened but captured only silence — check the input device and the "
                 "OS microphone permission for your terminal")
    except Exception as e:  # noqa: BLE001
        bad(f"mic capture failed: {e} — grant microphone permission to your terminal "
            "(macOS: System Settings → Privacy & Security → Microphone · "
            "Windows: Settings → Privacy & security → Microphone)")

    head("Benchmarks")
    pcm = bytearray()
    try:
        from tts import EdgeTTS
        from pipecat.frames.frames import TTSAudioRawFrame
        svc = EdgeTTS(voice=C.EDGE_VOICE, sample_rate=16000)
        svc._sample_rate = 16000  # 16k mono → feeds whisper directly (no re-decode)
        t = time.time()
        first = None
        async for fr in svc.run_tts("Testing the voice stack, one two three.", "doctor"):
            if isinstance(fr, TTSAudioRawFrame):
                if first is None:
                    first = (time.time() - t) * 1000
                pcm.extend(fr.audio)
        if first is not None:
            (ok if first < 1500 else warn)(f"edge-tts time-to-first-audio: {first:.0f} ms (streaming)")
        else:
            warn("edge-tts produced no audio")
    except Exception as e:  # noqa: BLE001
        warn(f"edge-tts failed: {e} (check your internet connection)")

    if pcm and model.exists() and (has_server or has_cli):
        from stt import _wav
        if has_server:
            try:
                import httpx
                from stt import ensure_server
                base = await ensure_server(model=str(model), port=C.WHISPER_PORT, language=C.WHISPER_LANG)
                t = time.time()
                async with httpx.AsyncClient(timeout=30) as cl:
                    r = await cl.post(base + "/inference",
                                      files={"file": ("a.wav", _wav(bytes(pcm)), "audio/wav")},
                                      data={"language": C.WHISPER_LANG, "response_format": "json",
                                            "no_timestamps": "true"})
                ms = (time.time() - t) * 1000
                txt = " ".join(r.json().get("text", "").split())
                (ok if ms < 1500 else warn)(f"STT whisper-server (resident): {ms:.0f} ms")
                dim(f'heard: "{txt}"')
            except Exception as e:  # noqa: BLE001
                warn(f"whisper-server benchmark failed: {e}")
        else:
            import tempfile, wave
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); f.close()
            with wave.open(f.name, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(pcm))
            t = time.time()
            out = subprocess.run(["whisper-cli", "-m", str(model), "-f", f.name,
                                  "-l", C.WHISPER_LANG, "-nt", "-np"], capture_output=True)
            ms = (time.time() - t) * 1000
            os.unlink(f.name)
            warn(f"STT whisper-cli (reloads model each turn): {ms:.0f} ms — install whisper-server to make this ~3x faster")
            dim(f'heard: "{" ".join(out.stdout.decode(errors="ignore").split())}"')

    head("Cost")
    dim("headless `claude -p` draws from your plan's monthly agent credit at API rates (since 2026-06-15)")
    dim("cheaper: CALL_MODEL=haiku + CALL_CONTINUE=0 — see README → Cost & billing")

    head("Verdict")
    if R_["fail"]:
        bad(f"{R_['fail']} problem(s) — fix the ✗ above before calling.")
    elif R_["warn"]:
        warn(f"{R_['warn']} warning(s) — it works, but see the ! above.")
    else:
        ok("All green. You're optimized — run `claude-call` from a project. 📞")
    sys.exit(1 if R_["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
