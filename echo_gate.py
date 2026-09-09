"""EchoGate — anti-eco half-duplex, sem fone e sem dependencia comercial.

Sem fone, o mic capta a propria voz do agente e ele se responde num loop. Filtros de
ruido (rnnoise/krisp) NAO resolvem: fala limpa nao e ruido. A solucao confiavel sem
hardware/licenca e half-duplex: enquanto o agente FALA, o mic e silenciado (descarta o
audio de entrada antes do VAD). Trade-off: nao da pra interromper a fala dele sem fone
(com fone, CALL_ECHO_GATE=0 traz o barge-in de volta).

Posicao no pipeline: logo depois de transport.input(), antes do VAD.
"""
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame, InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


class EchoGate(FrameProcessor):
    def __init__(self, *, tail_secs: float = 0.4, ui=None, anti_echo: bool = True):
        super().__init__()
        self._tail = tail_secs
        self._ui = ui                      # mute do usuario (tecla M/F9) fecha o mic aqui
        self._anti_echo = anti_echo        # half-duplex anti-eco (off no modo AEC/fone)
        self._bot_speaking = False
        self._unmute_at = 0.0

    def _user_muted(self) -> bool:
        # mute do usuário FECHA o mic de verdade: descarta o áudio antes do VAD/STT, então
        # nada é transcrito nem executado enquanto mutado (pedido explícito do usuário).
        return bool(self._ui and self._ui.muted)

    def _echo_muted(self) -> bool:
        # anti-eco: enquanto ela fala (e um rabicho depois), o mic fica mudo.
        return self._anti_echo and (self._bot_speaking or time.monotonic() < self._unmute_at)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._unmute_at = time.monotonic() + self._tail
        if isinstance(frame, InputAudioRawFrame) and (self._user_muted() or self._echo_muted()):
            return
        await self.push_frame(frame, direction)
