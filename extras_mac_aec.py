"""Transport Pipecat que fala com o aecbridge (AEC do macOS) via stdio.

Substitui o LocalAudioTransport (PyAudio) quando CALL_AEC=1. Um UNICO processo
aecbridge captura o mic (ja com cancelamento de eco) e toca o TTS — por isso da pra
interromper o agente SEM FONE. PCM = 24 kHz mono Int16 LE nos dois sentidos.

Requisitos: binario `aecbridge` compilado (build.sh) e permissao de microfone no
Terminal/app que roda o bot.
"""
import asyncio
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

BRIDGE_BIN = str(Path(__file__).resolve().parent / "aecbridge")
MIC_RATE = 16000  # mic -> Python (VAD/whisper querem 16k)
SPK_RATE = 24000  # Python (TTS) -> playback


class MacAECTransportParams(TransportParams):
    pass


class _BridgeProc:
    """Dono do subprocess aecbridge, compartilhado por input e output."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def ensure(self) -> subprocess.Popen:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return self.proc
            if not Path(BRIDGE_BIN).exists():
                raise FileNotFoundError(f"aecbridge nao compilado: {BRIDGE_BIN} (rode ./build.sh)")
            self.proc = subprocess.Popen(
                [BRIDGE_BIN],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
            threading.Thread(target=self._drain_stderr, daemon=True).start()
            logger.info(f"[aecbridge] iniciado (pid={self.proc.pid})")
            return self.proc

    def _drain_stderr(self):
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode(errors="ignore").rstrip()
            if line:
                logger.info(f"[aecbridge] {line}")

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class MacAECInputTransport(BaseInputTransport):
    def __init__(self, bridge: _BridgeProc, params: MacAECTransportParams):
        super().__init__(params)
        self._bridge = bridge
        self._reader: threading.Thread | None = None
        self._running = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._running:
            return
        self._bridge.ensure()
        self._running = True
        loop = self.get_event_loop()
        self._reader = threading.Thread(target=self._read_loop, args=(loop,), daemon=True)
        self._reader.start()
        await self.set_transport_ready(frame)

    def _read_loop(self, loop):
        proc = self._bridge.proc
        frame_bytes = int(MIC_RATE / 100) * 2  # 20ms mono int16 @ 16k
        while self._running and proc and proc.poll() is None:
            data = proc.stdout.read(frame_bytes)
            if not data:
                break
            frame = InputAudioRawFrame(audio=data, sample_rate=MIC_RATE, num_channels=1)
            asyncio.run_coroutine_threadsafe(self.push_audio_frame(frame), loop)

    async def cleanup(self):
        await super().cleanup()
        self._running = False


class MacAECOutputTransport(BaseOutputTransport):
    def __init__(self, bridge: _BridgeProc, params: MacAECTransportParams):
        super().__init__(params)
        self._bridge = bridge
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._bridge.ensure()
        await self.set_transport_ready(frame)

    def _write(self, data: bytes):
        proc = self._bridge.proc
        if proc and proc.poll() is None and proc.stdin:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        await self.get_event_loop().run_in_executor(self._executor, self._write, frame.audio)
        return True


class MacAECTransport(BaseTransport):
    """Transport full-duplex com AEC do macOS (Voice Processing I/O via aecbridge)."""

    def __init__(self, params: MacAECTransportParams | None = None):
        super().__init__()
        self._params = params or MacAECTransportParams(
            audio_in_enabled=True, audio_out_enabled=True,
            audio_in_sample_rate=MIC_RATE, audio_out_sample_rate=SPK_RATE,
        )
        self._bridge = _BridgeProc()
        self._input: MacAECInputTransport | None = None
        self._output: MacAECOutputTransport | None = None

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = MacAECInputTransport(self._bridge, self._params)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = MacAECOutputTransport(self._bridge, self._params)
        return self._output
