"""AudioMeter — mede o nível do áudio de entrada (mic) em tempo real e manda pro painel,
pra wave animar SINCRONIZADA com o som real (prova que o mic está funcionando).

Posição no pipeline: logo após transport.input(), ANTES do echo gate — assim vê o mic
sempre (mesmo mutado/durante a fala dela), refletindo o áudio de verdade.
"""
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class AudioMeter(FrameProcessor):
    def __init__(self, ui, *, gain: float = 9.0):
        super().__init__()
        self._ui = ui
        self._gain = gain

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and self._ui is not None:
            try:
                if self._ui.muted:                           # mutado: wave zerada = mic fechado
                    self._ui.set_level(0.0)
                else:
                    rms = audioop.rms(frame.audio, 2)        # RMS do PCM 16-bit
                    lvl = (rms / 32768.0) * self._gain
                    self._ui.set_level(min(1.0, lvl ** 0.7)) # curva perceptual + clamp
            except Exception:  # noqa: BLE001
                pass
        await self.push_frame(frame, direction)
