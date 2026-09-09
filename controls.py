"""Controles ao vivo da call, por tecla, mostrados no painel (mid-call).

Teclas:
  M / espaço  mutar (para de ouvir)
  I           interromper a fala dele AGORA
  C           abre/fecha o painel de CONFIG
  V           cola clipboard no buffer de staging (imagem ou texto)
  ↵ Enter     envia tudo do buffer de staging de uma vez
  + / -       sensibilidade do mic (VAD) — ao vivo (menos ruido <-> ouve mais)
  [ / ]       trocar dispositivo de entrada (mic) — aplica no proximo start
  B           barge-in por voz on/off (interromper falando; precisa fone/AEC)

Cola múltiplos itens (V, V, V…) e só envia ao pressionar ENTER.
Cada imagem salva com nome único (/tmp/claude-call-clip-N.png) — sem sobrescrever.
VAD muda ao vivo (set_params). Mic e barge-in sao persistidos no .env.
"""
import re
from pathlib import Path

from loguru import logger

from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import InterruptionTaskFrame
from pipecat.processors.frame_processor import FrameDirection

ENV_PATH = Path(__file__).resolve().parent / ".env"

# niveis de sensibilidade do mic (VAD): (confidence, min_volume). MENOR = ouve mais (e mais ruido).
# Nivel 2 == defaults do upstream/Silero (o que respondia melhor).
_SENS = [
    (0.75, 0.50),   # 0 — só fala bem alta (ignora ruído)
    (0.62, 0.35),   # 1
    (0.50, 0.20),   # 2 — PADRÃO (capta fala normal do mic do MacBook)
    (0.40, 0.12),   # 3
    (0.30, 0.06),   # 4 — pega fala bem baixa (mais sensível a ruído)
]

# "filtro de ruído" = start_secs (quanto de fala sustentada p/ disparar; maior ignora blip)
_NOISE = [0.10, 0.20, 0.35]   # 0=off, 1=médio, 2=alto

# chaves que o reset remove do .env (volta pros defaults bons)
_RESET_KEYS = ("CALL_VAD_CONFIDENCE", "CALL_VAD_MIN_VOLUME", "CALL_VAD_STOP_SECS",
               "CALL_VAD_START_SECS", "CALL_INPUT_DEVICE", "CALL_ECHO_GATE", "CALL_ECHO_TAIL")


# dispositivos virtuais (sem áudio real) — nunca usar/ciclar nesses
_VIRTUAL = ("blackhole", "aggregate", "multi-output", "soundflower", "loopback", "quicktime", "cam link", "camlink", "elgato", "iphone", "ipad")


def _is_virtual(name: str) -> bool:
    n = (name or "").lower()
    return any(v in n for v in _VIRTUAL)


def list_input_devices():
    """Mics REAIS (exclui BlackHole e afins)."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        out = []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d.get("maxInputChannels", 0) > 0 and not _is_virtual(d.get("name", "")):
                out.append((i, d["name"]))
        pa.terminate()
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[controls] devices: {e}")
        return []


def builtin_mic_index():
    """Index do mic do COMPUTADOR (MacBook/built-in), nunca virtual. None = default do SO."""
    devs = list_input_devices()
    for i, name in devs:
        n = name.lower()
        if "macbook" in n or "built-in" in n or "internal" in n or "interno" in n:
            return i
    return devs[0][0] if devs else None


def resolve_device(name):
    """Index ATUAL do device pelo NOME salvo (índices do pyaudio mudam — por isso por nome).
    Sem nome ou não achou -> mic do computador."""
    if name and not str(name).isdigit():  # ignora índices antigos persistidos
        for i, n in list_input_devices():
            if name.lower() in n.lower() or n.lower() in name.lower():
                return i
    return builtin_mic_index()


def sens_from_confidence(conf: float) -> int:
    return min(range(len(_SENS)), key=lambda i: abs(_SENS[i][0] - conf))


def _set_env(key, value):
    try:
        lines = ENV_PATH.read_text().splitlines()
    except OSError:
        lines = []
    pat = re.compile(rf"^#?\s*{re.escape(key)}=")
    for i, l in enumerate(lines):
        if pat.match(l):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    try:
        ENV_PATH.write_text("\n".join(lines) + "\n")
    except OSError as e:  # noqa: BLE001
        logger.error(f"[controls] persist {key}: {e}")


def _del_env(*keys):
    """Remove linhas CALL_X do .env (volta pro default do config)."""
    try:
        lines = ENV_PATH.read_text().splitlines()
    except OSError:
        return
    pats = [re.compile(rf"^#?\s*{re.escape(k)}=") for k in keys]
    kept = [l for l in lines if not any(p.match(l) for p in pats)]
    try:
        ENV_PATH.write_text("\n".join(kept) + "\n")
    except OSError as e:  # noqa: BLE001
        logger.error(f"[controls] reset env: {e}")


class Controls:
    def __init__(self, *, ui, vad_analyzer, task, brain, loop,
                 sens_level, start_secs, stop_secs, device_index):
        self.ui = ui
        self.vad = vad_analyzer
        self.task = task
        self.brain = brain
        self.loop = loop
        self.sens = sens_level
        self.start_secs = start_secs
        self.stop_secs = stop_secs
        self.noise = min(range(len(_NOISE)), key=lambda i: abs(_NOISE[i] - start_secs))
        self.devices = list_input_devices()
        self.device_index = device_index
        self.barge_in = False
        self.typing = False
        self.buf = ""
        self._staged: list[tuple[str, str]] = []  # ("image"|"text", value)
        self._clip_counter = 0
        self._sync_ui()

    def handle(self, s: str):
        if self.typing:
            self._type_input(s)
            return
        for ch in s:
            self._command(ch)

    def _command(self, ch: str):
        if ch in ("\r", "\n"):
            self.flush_staged()
            return
        if ch in (" ", "m", "M"):
            self.ui.toggle_mute()
        elif ch in ("i", "I"):
            self.interrupt()
        elif ch in ("c", "C"):
            self.ui.toggle_settings()
        elif ch in ("v", "V"):
            self.paste()
        elif ch in ("t", "T"):
            self.start_typing()
        elif ch in ("+", "="):
            self.set_sens(self.sens + 1)
        elif ch in ("-", "_"):
            self.set_sens(self.sens - 1)
        elif ch == ".":
            self.set_stop(self.stop_secs + 0.2)   # espera mais (anti-atropelo)
        elif ch == ",":
            self.set_stop(self.stop_secs - 0.2)
        elif ch in ("n", "N"):
            self.set_noise((self.noise + 1) % len(_NOISE))   # cicla filtro de ruído
        elif ch in ("e", "E"):
            self.toggle_code_effort()    # effort do daemon de código (xhigh <-> ultracode/max)
        elif ch in ("b", "B"):
            # só sessão/display: NÃO persiste no .env (default é sempre OFF).
            # Pra barge-in de verdade: setar CALL_ECHO_GATE=0 no .env à mão (precisa fone).
            self.barge_in = not self.barge_in
            self._sync_ui()
        elif ch in ("r", "R"):
            self.reset()

    def reset(self):
        """Volta TUDO pro padrão: remove overrides do .env, VAD nível 2 (upstream),
        mic = computador (default do sistema), barge-in OFF, desmuta."""
        _del_env(*_RESET_KEYS)
        self.sens = 2
        self.stop_secs = 1.0
        self.start_secs = 0.2
        self.noise = min(range(len(_NOISE)), key=lambda i: abs(_NOISE[i] - self.start_secs))
        self.device_index = None         # default = mic do computador
        self.barge_in = False
        self.devices = list_input_devices()
        conf, minv = _SENS[2]
        try:
            self.vad.set_params(VADParams(confidence=conf, start_secs=self.start_secs,
                                          stop_secs=self.stop_secs, min_volume=minv))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[controls] reset vad: {e}")
        if self.ui is not None:
            self.ui.muted = False
        self._sync_ui()
        if self.ui is not None:
            self.ui.status("ouvindo")
            self.ui.hint("config resetada pro padrão (mic = computador). troca de mic aplica ao reiniciar.")

    # ---------- colar / escrever (injeta texto na conversa) ----------
    def send_text(self, text: str):
        text = (text or "").strip()
        if not text or self.brain is None:
            return
        try:
            self.loop.create_task(self.brain.inject_text(text))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[controls] send_text: {e}")

    def paste(self):
        """Cola o clipboard no buffer de staging. ENTER envia tudo junto.
        macOS: pbpaste (texto) + osascript (imagem). Windows: Get-Clipboard (texto)."""
        img = self._clip_image()
        if img:
            self._staged.append(("image", img))
            self._show_staged_hint()
            return
        try:
            import os
            import subprocess
            if os.name == "nt":
                txt = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=5).stdout
            else:
                txt = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout
        except Exception as e:  # noqa: BLE001
            logger.error(f"[controls] clipboard: {e}")
            return
        txt = " ".join(txt.split())
        if txt:
            self._staged.append(("text", txt))
            self._show_staged_hint()
        elif self.ui:
            self.ui.hint("clipboard vazio")

    def _show_staged_hint(self):
        if not self.ui:
            return
        imgs = sum(1 for t, _ in self._staged if t == "image")
        txts = sum(1 for t, _ in self._staged if t == "text")
        parts = []
        if imgs:
            parts.append(f"{imgs} imagem{'ns' if imgs > 1 else ''}")
        if txts:
            parts.append(f"{txts} texto{'s' if txts > 1 else ''}")
        self.ui.hint(f"📋 buffer: {', '.join(parts)} — ↵ pra enviar, V pra adicionar mais")

    def flush_staged(self):
        """Envia tudo que está no buffer de staging de uma vez."""
        if not self._staged:
            return
        staged = self._staged[:]
        self._staged.clear()

        imgs = [v for t, v in staged if t == "image"]
        txts = [v for t, v in staged if t == "text"]

        parts = []
        if imgs:
            img_list = ", ".join(imgs)
            label = "imagens" if len(imgs) > 1 else "imagem"
            parts.append(f"Colei {len(imgs)} {label} pra você analisar. "
                         f"Lê {'os arquivos' if len(imgs) > 1 else 'o arquivo'}: {img_list}")
        if txts:
            parts.append("Texto colado:\n" + "\n\n".join(txts))

        message = "\n\n".join(parts)
        self.send_text(message)
        if self.ui:
            summary = f"{len(imgs)}img + {len(txts)}txt" if imgs and txts else \
                      (f"{len(imgs)} imagem" + ("ns" if len(imgs) > 1 else "") if imgs
                       else f"{len(txts)} texto" + ("s" if len(txts) > 1 else ""))
            self.ui.hint(f"📤 enviado: {summary}")

    def _clip_image(self):
        """Se o clipboard tem imagem, grava PNG com nome único e retorna o caminho (macOS)."""
        import subprocess
        import sys
        if sys.platform != "darwin":
            return None
        self._clip_counter += 1
        path = f"/tmp/claude-call-clip-{self._clip_counter}.png"
        script = ("try\n"
                  " set thePng to (the clipboard as «class PNGf»)\n"
                  f" set fp to open for access POSIX file \"{path}\" with write permission\n"
                  " set eof fp to 0\n"
                  " write thePng to fp\n"
                  " close access fp\n"
                  " return \"ok\"\n"
                  "on error\n return \"no\"\nend try")
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=5)
            if "ok" in (r.stdout or ""):
                return path
        except Exception as e:  # noqa: BLE001
            logger.error(f"[controls] clip image: {e}")
        return None

    def start_typing(self):
        self.typing = True
        self.buf = ""
        if self.ui:
            self.ui.set_typing(True, "")

    def _stop_typing(self):
        self.typing = False
        self.buf = ""
        if self.ui:
            self.ui.set_typing(False, "")

    def _type_input(self, s: str):
        if len(s) == 1:
            ch = s
            if ch in "\r\n":               # Enter = envia
                txt = self.buf
                self._stop_typing()
                self.send_text(txt)
                return
            if ch == "\x1b":               # Esc = cancela
                self._stop_typing()
                return
            if ch in "\x7f\b":             # backspace
                self.buf = self.buf[:-1]
            elif ch >= " ":                # imprimível
                self.buf += ch
        else:                              # blob (paste no terminal) -> anexa
            self.buf += " ".join(s.replace("\r", "\n").split("\n"))
        if self.ui:
            self.ui.set_typing(True, self.buf)

    def interrupt(self):
        """Para AGORA: o turno do daemon (geração+tools, via control_request) E a fala
        (flush do TTS via InterruptionTaskFrame)."""
        try:
            self.loop.create_task(self.brain.interrupt_turn())
            self.loop.create_task(
                self.task.queue_frame(InterruptionTaskFrame(), FrameDirection.UPSTREAM))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[controls] interrupt: {e}")
        self.ui.status("ouvindo")

    def _apply_vad(self):
        # SÓ sessão: NÃO persiste no .env (senão drifta e trava num estado ruim).
        # Todo start nasce no default bom do config. Pra fixar, editar CALL_VAD_* à mão.
        conf, minv = _SENS[self.sens]
        try:
            self.vad.set_params(VADParams(confidence=conf, start_secs=self.start_secs,
                                          stop_secs=self.stop_secs, min_volume=minv))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[controls] vad set_params: {e}")
        self._sync_ui()

    def set_sens(self, level: int):
        self.sens = max(0, min(len(_SENS) - 1, level))
        self._apply_vad()

    def set_stop(self, secs: float):
        self.stop_secs = max(0.2, min(2.5, round(secs, 2)))
        self._apply_vad()

    def set_noise(self, level: int):
        self.noise = max(0, min(len(_NOISE) - 1, level))
        self.start_secs = _NOISE[self.noise]
        self._apply_vad()

    def toggle_code_effort(self):
        """Alterna o effort do daemon de PROGRAMAR (voz não muda): xhigh <-> ultracode(max)."""
        cur = getattr(self.brain, "_code_effort", "xhigh") or "xhigh"
        nxt = "xhigh" if cur == "max" else "max"
        if self.brain is not None:
            self.brain._code_effort = nxt
        _set_env("CALL_CODE_EFFORT", nxt)
        self._sync_ui()
        if self.ui:
            self.ui.hint(f"effort código → {'ultracode (max)' if nxt == 'max' else 'xhigh'}")

    def _device_name(self):
        return next((n for i, n in self.devices if i == self.device_index), "default")

    def _sync_ui(self):
        self.ui.set_settings(sens=self.sens, sens_max=len(_SENS) - 1,
                             device=self._device_name(), barge_in=self.barge_in,
                             stop_secs=self.stop_secs, noise=self.noise, noise_max=len(_NOISE) - 1,
                             code_effort=getattr(self.brain, "_code_effort", "xhigh"))
