"""Sons curtos de feedback, não-bloqueantes (macOS: afplay · Windows: winsound).

- heard: captei sua fala, vou executar
- on:    mic ATIVADO (voltou a ouvir)
- off:   mic MUTADO
Desliga com CALL_SOUNDS=0.
"""
import os
import subprocess
import sys
from pathlib import Path

_DIR = Path("/System/Library/Sounds")
_MAP = {"heard": "Pop.aiff", "wake": "Ping.aiff", "on": "Hero.aiff", "off": "Bottle.aiff", "done": "Tink.aiff"}
# Linux: nessun suono di sistema equivalente pronto — usa il tema freedesktop (.oga),
# decodificato al volo con ffmpeg (gia' dipendenza del progetto) e suonato con aplay.
_LINUX_DIR = Path("/usr/share/sounds/freedesktop/stereo")
_LINUX_MAP = {"heard": "message-new-instant.oga", "wake": "bell.oga",
              "on": "device-added.oga", "off": "device-removed.oga", "done": "complete.oga"}
_ON = os.getenv("CALL_SOUNDS", "1") not in ("0", "false", "no")


def play(kind: str):
    if not _ON:
        return
    if sys.platform == "darwin":
        f = _DIR / _MAP.get(kind, "Pop.aiff")
        try:
            subprocess.Popen(["afplay", str(f)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
        return
    if sys.platform.startswith("linux"):
        f = _LINUX_DIR / _LINUX_MAP.get(kind, "message.oga")
        if not f.exists():
            return
        try:
            subprocess.Popen(
                f"ffmpeg -loglevel quiet -nostdin -i '{f}' -f wav - | aplay -q -",
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass
        return
    if os.name == "nt":
        # winsound.MessageBeep é assíncrono (retorna na hora) — sons de sistema distintos
        try:
            import winsound
            beeps = {"heard": winsound.MB_OK, "wake": winsound.MB_ICONASTERISK,
                     "on": winsound.MB_ICONASTERISK, "off": winsound.MB_ICONHAND,
                     "done": winsound.MB_OK}
            winsound.MessageBeep(beeps.get(kind, winsound.MB_OK))
        except Exception:  # noqa: BLE001
            pass
