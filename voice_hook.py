#!/usr/bin/env python3
"""Stop-hook → voice loop on the LIVE interactive Claude Code session (NO `-p`).

Wired as a `Stop` hook. On each stop, if call mode is active (flag file exists):
  1. speaks the assistant's last message (pt-BR edge-tts)
  2. records the mic until you go quiet, transcribes it (whisper)
  3. returns {"decision":"block","reason": <what you said>} so the SAME session
     keeps going, with your spoken words as the next turn.
Goes silent or you say a stop word ("encerrar", "tchau") → it lets the turn stop.

Billing: these are INTERACTIVE turns (your plan's flat usage), not headless `-p`.
"""
import json
import os
import sys
import time
import wave
from pathlib import Path

FLAG = Path.home() / ".claude-call-active"          # call mode on/off
STOP_WORDS = ("encerrar", "encerra a call", "tchau", "desliga", "end call", "para a call",
              "fine chiamata", "chiudi la chiamata", "basta cosi", "basta così", "chiudi")
SAMPLE_RATE = 16000


def _allow_stop():
    # exit 0 with no JSON → the turn is allowed to stop normally
    sys.exit(0)


def _record_until_silence(silence_secs=1.3, max_secs=25, thresh=480):
    import audioop
    import pyaudio
    pa = pyaudio.PyAudio()
    st = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE, input=True,
                 frames_per_buffer=1024)
    frames, silent, started, t0 = [], 0.0, False, time.time()
    try:
        while time.time() - t0 < max_secs:
            data = st.read(1024, exception_on_overflow=False)
            frames.append(data)
            rms = audioop.rms(data, 2)
            if rms > thresh:
                started, silent = True, 0.0
            elif started:
                silent += 1024 / SAMPLE_RATE
            if started and silent > silence_secs:
                break
    finally:
        st.stop_stream(); st.close(); pa.terminate()
    return b"".join(frames) if started else b""


async def _speak(text: str):
    import edge_tts
    import config as C
    from brain import _clean_for_speech
    clean = _clean_for_speech(text)[:1200]  # don't read a wall of markdown
    if not clean:
        return
    import tempfile, subprocess
    mp3 = bytearray()
    # sempre voz edge: C.VOICE pode ser um voice id de provider premium (ElevenLabs...)
    voice = C.VOICE if C.TTS == "edge" else C.EDGE_VOICE
    async for c in edge_tts.Communicate(clean, voice, rate=C.VOICE_RATE).stream():
        if c["type"] == "audio":
            mp3.extend(c["data"])
    f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False); f.write(mp3); f.close()
    subprocess.run(["afplay", f.name] if sys.platform == "darwin"
                   else ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", f.name],
                   check=False)
    os.unlink(f.name)


async def _transcribe(pcm: bytes) -> str:
    import httpx
    import config as C
    from stt import ensure_server, _wav
    base = await ensure_server(model=C.WHISPER_MODEL, port=C.WHISPER_PORT, language=C.WHISPER_LANG)
    async with httpx.AsyncClient(timeout=30) as cl:
        r = await cl.post(base + "/inference",
                          files={"file": ("a.wav", _wav(pcm), "audio/wav")},
                          data={"language": C.WHISPER_LANG, "response_format": "json",
                                "no_timestamps": "true", "temperature": "0"})
    return " ".join(r.json().get("text", "").split()).strip()


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        _allow_stop()

    if not FLAG.exists():
        _allow_stop()  # call mode off → do nothing, let it stop

    import asyncio
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    # 1) speak my reply
    msg = (data or {}).get("last_assistant_message") or ""
    try:
        if msg:
            asyncio.run(_speak(msg))
    except Exception:
        pass

    # 2) listen
    try:
        pcm = _record_until_silence()
    except Exception:
        _allow_stop()
    if not pcm:
        _allow_stop()  # you stayed quiet → end the call

    # 3) transcribe
    try:
        said = asyncio.run(_transcribe(pcm))
    except Exception:
        _allow_stop()
    low = said.lower()
    if not said or any(w in low for w in STOP_WORDS):
        try:
            FLAG.unlink()
        except OSError:
            pass
        _allow_stop()

    # 4) feed your words back as the next turn (interactive — no -p)
    import config as C
    reason = f"{said}\n\n({C.VOICE_RULES})"
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


if __name__ == "__main__":
    main()
