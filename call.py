"""claude-call — uma ligacao de voz com o seu Claude Code.

Pipeline (Pipecat):
  mic -> [EchoGate] -> VAD (Silero) -> whisper (STT local)
      -> ClaudeBrain (daemon `claude` retomando a SUA sessao) -> edge-tts -> alto-falante

Cerebro = `claude` CLI ja autenticado (sua assinatura). Nao precisa de API key.
Rodar:  ./call.sh   (ou: uv run python call.py)
"""
import asyncio
import os
import sys
import warnings

# Warnings do pipecat (PipelineTask/Runner deprecated) escrevem no stderr e CORROMPEM
# o painel rich Live. Silencia antes de qualquer import pesado.
warnings.filterwarnings("ignore")
# Propaga p/ subprocessos (o resource_tracker do multiprocessing tem filtro proprio e
# senao imprime 'leaked semaphore' no shutdown — vem do torch/Silero VAD, inofensivo).
os.environ.setdefault("PYTHONWARNINGS", "ignore")

from dotenv import load_dotenv
load_dotenv()

from loguru import logger  # noqa: E402

from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams  # noqa: E402
from pipecat.frames.frames import TTSSpeakFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.audio.vad_processor import VADProcessor  # noqa: E402
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams  # noqa: E402

import config as C  # noqa: E402
from brain import ClaudeBrain  # noqa: E402
from echo_gate import EchoGate  # noqa: E402
from session import latest_session  # noqa: E402
from stt import make_stt  # noqa: E402
from tts import make_tts  # noqa: E402
from ui import CallUI  # noqa: E402

_FILLERS = {
    "en": ["One sec.", "Hold on.", "Let me check.", "Give me a moment."],
    "pt": ["Peraí.", "Deixa eu ver.", "Um segundo.", "Já te falo."],
    "es": ["Un momento.", "Déjame ver.", "Espera.", "Ya te digo."],
}

# Frase falada quando o watchdog recupera de um travamento (recomeço fresh).
_RECOVER = {
    "en": "I got stuck and restarted — could you say that again?",
    "pt": "Travei e recomecei do zero. Pode repetir?",
    "es": "Me trabé y reinicié. ¿Puedes repetir?",
}


def _setup_keys(handler, loop):
    """Lê teclas e manda pro handler. POSIX: cbreak + add_reader (mantém ISIG/Ctrl+C).
    Windows: termios não existe e o Proactor loop não tem add_reader — uma thread com
    msvcrt entrega as teclas via call_soon_threadsafe (mesmas teclas M/I/T/± etc.)."""
    if not sys.stdin.isatty():
        return None

    if os.name == "nt":
        import msvcrt
        import threading
        import time as _time
        stop = threading.Event()

        def _reader():
            while not stop.is_set():
                if not msvcrt.kbhit():
                    _time.sleep(0.03)
                    continue
                try:
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):   # tecla especial (setas/F#) vem em par
                        msvcrt.getwch()
                        continue
                except Exception:  # noqa: BLE001
                    continue

                def _dispatch(c=ch):
                    try:
                        handler(c)
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"[keys] {e}")
                loop.call_soon_threadsafe(_dispatch)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        return ("nt", stop)

    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    def on_key():
        try:
            # lê o que tiver disponível (suporta acentos UTF-8 e paste no terminal)
            s = os.read(fd, 4096).decode(errors="ignore")
        except OSError:
            return
        if s:
            try:
                handler(s)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[keys] {e}")

    loop.add_reader(fd, on_key)
    return (fd, old)


def _restore_keys(state, loop):
    if not state:
        return
    if state[0] == "nt":         # Windows: só para a thread do msvcrt
        state[1].set()
        return
    fd, old = state
    try:
        loop.remove_reader(fd)
    except Exception:  # noqa: BLE001
        pass
    import termios
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:  # noqa: BLE001
        pass


async def main():
    # Sessao: continua a SUA conversa mais recente desse diretorio (o diferencial).
    session_id = C.SESSION_ID
    if not session_id and C.CONTINUE:
        session_id = latest_session(C.CWD)
    if session_id:
        logger.info(f"continuando sua sessao do Claude Code: {session_id}")
    else:
        logger.info("sessao nova (nenhuma conversa anterior encontrada nesse diretorio)")

    # Feedback visual (painel ao vivo). Em terminal real, manda os logs pro arquivo
    # pra nao sujar o painel; sem TTY (background) a UI se desliga e os logs ficam.
    ui = CallUI(session_id=session_id, cwd=C.CWD, lang=C.LANG, name=C.NAME) if C.UI else None

    # Transcript da call (sempre on — independe do painel): transcripts/<data>.md
    from transcript import Transcript
    transcript = Transcript(name=C.NAME, session_id=session_id or "", lang=C.LANG, cwd=C.CWD)
    if ui and ui.enabled:
        # loguru -> arquivo (terminal limpo pro painel). NAO mexer em sys.stderr: trocar
        # o fd quebra o signal wakeup fd do asyncio e TRAVA a leitura do subprocess.
        # Os warnings que sujariam o painel ja sao filtrados por warnings.filterwarnings.
        logger.remove()
        logger.add("logs/claude-call.log", level="DEBUG", rotation="5 MB", enqueue=True)
        logger.add("logs/errors.log", level="ERROR", rotation="5 MB", enqueue=True)

    # Transport
    use_echo_gate = C.ECHO_GATE
    from controls import builtin_mic_index, resolve_device
    # Mic por NOME (CALL_INPUT_DEVICE) — estavel a mudanca de indice / iPhone Continuity.
    # Sem nome -> mic built-in/computador. (Mac Mini c/ Scarlett: setar CALL_INPUT_DEVICE.)
    input_dev = resolve_device(C.INPUT_DEVICE_NAME) if C.INPUT_DEVICE_NAME else builtin_mic_index()
    if C.AEC:
        from extras_mac_aec import MacAECTransport  # opcional, so macOS
        transport = MacAECTransport()
        use_echo_gate = False
        logger.info("transport: macOS AEC (Voice Processing I/O)")
    else:
        tp = LocalAudioTransportParams(
            audio_in_enabled=True, audio_out_enabled=True,
            audio_in_sample_rate=C.SAMPLE_RATE_IN, audio_out_sample_rate=C.SAMPLE_RATE_OUT,
        )
        # device já resolvido por nome acima (input_dev)
        if input_dev is not None:
            tp.input_device_index = input_dev
            logger.info(f"mic: device #{input_dev}")
        transport = LocalAudioTransport(tp)

    vad_analyzer = SileroVADAnalyzer(params=VADParams(
        confidence=C.VAD_CONFIDENCE, start_secs=C.VAD_START_SECS,
        stop_secs=C.VAD_STOP_SECS, min_volume=C.VAD_MIN_VOLUME))
    vad = VADProcessor(vad_analyzer=vad_analyzer)

    stt = await make_stt(provider=C.STT, api_key=C.STT_API_KEY, api_model=C.STT_MODEL,
                         model=C.WHISPER_MODEL, language=C.WHISPER_LANG,
                         port=C.WHISPER_PORT, use_server=C.USE_WHISPER_SERVER)

    brain = ClaudeBrain(
        voice_rules=C.VOICE_RULES, model=C.MODEL, effort=C.EFFORT,
        code_model=C.CODE_MODEL, code_effort=C.CODE_EFFORT, permission_flag=C.PERMISSION,
        wake_words=C.WAKE, active_window_secs=C.ACTIVE_WINDOW,
        fillers=_FILLERS.get(C.LANG[:2], _FILLERS["en"]),
        session_id=session_id, cwd=C.CWD,
        first_resp_timeout=C.FIRST_RESP_TIMEOUT, stall_timeout=C.TURN_TIMEOUT,
        tool_timeout=C.TOOL_TIMEOUT,
        recover_phrase=_RECOVER.get(C.LANG[:2], _RECOVER["en"]),
        lang=C.LANG,
        ui=ui,
        transcript=transcript,
    )

    try:
        tts = make_tts(provider=C.TTS, voice=C.VOICE, rate=C.VOICE_RATE,
                       sample_rate=C.SAMPLE_RATE_OUT, api_key=C.TTS_API_KEY, model=C.TTS_MODEL)
        if C.TTS != "edge":
            logger.info(f"TTS: {C.TTS} (premium)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"TTS '{C.TTS}' unavailable ({e}); falling back to free edge-tts")
        tts = make_tts(provider="edge", voice=C.EDGE_VOICE, rate=C.VOICE_RATE,
                       sample_rate=C.SAMPLE_RATE_OUT)

    # Pre-aquece o cache de TTS das frases FIXAS (fillers + greeting) em background:
    # o 1o "Peraí." da call sai instantâneo em vez de pagar um RTT do edge-tts.
    from tts import EdgeTTS, prewarm_edge_cache
    if isinstance(tts, EdgeTTS):
        fixed = _FILLERS.get(C.LANG[:2], _FILLERS["en"]) + [C.GREETING]
        asyncio.create_task(prewarm_edge_cache(
            fixed, voice=C.VOICE if C.TTS == "edge" else C.EDGE_VOICE,
            rate=C.VOICE_RATE, sample_rate=C.SAMPLE_RATE_OUT))

    stages = [transport.input()]
    if ui and ui.enabled:
        from audio_meter import AudioMeter
        stages.append(AudioMeter(ui))   # wave = nível real do mic (antes do gate)
    # Gate SEMPRE no pipeline: mesmo sem anti-eco (AEC/fone), o mute do usuário precisa
    # fechar o mic de verdade. anti_echo=False = só corta no mute, não no bot falando.
    stages.append(EchoGate(tail_secs=C.ECHO_TAIL, ui=ui, anti_echo=use_echo_gate))
    stages += [vad, stt, brain, tts, transport.output()]
    pipeline = Pipeline(stages)

    # Idle timeout: por padrao 30 min de silencio; "0"/"off" = nunca encerra sozinha.
    idle_kwargs = ({"cancel_on_idle_timeout": False}
                   if C.IDLE_TIMEOUT.strip().lower() in ("0", "off", "none", "")
                   else {"idle_timeout_secs": float(C.IDLE_TIMEOUT)})
    task = PipelineTask(pipeline, params=PipelineParams(
        audio_in_sample_rate=C.SAMPLE_RATE_IN, audio_out_sample_rate=C.SAMPLE_RATE_OUT,
    ), **idle_kwargs)
    await task.queue_frames([TTSSpeakFrame(C.GREETING)])

    # No Windows (Proactor loop) add_signal_handler nao existe — o handle_sigint do
    # pipecat quebraria no start. Desligamos ele e tratamos o Ctrl+C na mao: cancela a
    # pipeline graciosamente (senao o KeyboardInterrupt deixa o shutdown sujo).
    runner = PipelineRunner(handle_sigint=(os.name != "nt"))
    if os.name == "nt":
        import signal
        loop = asyncio.get_running_loop()
        def _on_sigint(*_):
            loop.call_soon_threadsafe(lambda: loop.create_task(task.cancel(reason="Ctrl+C")))
        signal.signal(signal.SIGINT, _on_sigint)
    logger.info(f"claude-call no ar — voz={C.VOICE}, lang={C.LANG}, modelo={C.MODEL or 'default'}")
    keys = None
    hotkey_listener = None
    if ui:
        ui.start()
        if ui.enabled:
            from controls import Controls, sens_from_confidence
            controls = Controls(
                ui=ui, vad_analyzer=vad_analyzer, task=task, brain=brain,
                loop=asyncio.get_running_loop(),
                sens_level=sens_from_confidence(C.VAD_CONFIDENCE),
                start_secs=C.VAD_START_SECS, stop_secs=C.VAD_STOP_SECS,
                device_index=input_dev)
            keys = _setup_keys(controls.handle, asyncio.get_running_loop())
            # hotkey GLOBAL (sem foco no terminal): segura a tecla -> muta/desmuta.
            # Só importa pynput (~22MB) se o hotkey estiver ligado (CALL_HOTKEY != off).
            if C.HOTKEY:
                from hotkey import start_global_hotkey
                _loop = asyncio.get_running_loop()
                hotkey_listener = start_global_hotkey(
                    C.HOTKEY, C.HOTKEY_SECS,
                    lambda: _loop.call_soon_threadsafe(ui.toggle_mute))
    try:
        await runner.run(task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[call] erro fatal")
        if ui:
            ui.error(f"erro fatal: {e}", e)
            await asyncio.sleep(2.5)  # deixa o erro visivel antes de fechar o painel
        raise
    finally:
        _restore_keys(keys, asyncio.get_running_loop())
        if hotkey_listener is not None:
            try:
                hotkey_listener.stop()
            except Exception:  # noqa: BLE001
                pass
        if ui:
            ui.stop()
        transcript.close()
        if transcript.path:
            logger.info(f"transcript salvo: {transcript.path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
