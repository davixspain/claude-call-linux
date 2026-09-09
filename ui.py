"""Feedback visual da call — painel ao vivo + handoff em aba.

- Painel limpo em tela cheia (rich Live, alternate screen) que LIMPA a tela ao iniciar.
- Wave de voz animada enquanto OUVE / PENSA / FALA + mascote pixel.
- Mostra o que foi ouvido, cada tool (Edit/Bash/Read + alvo) e a resposta — sem JSON.
- Todo erro aparece em vermelho no painel E vai pra logs/errors.log (com traceback).
- Sob comando de voz, abre uma ABA do lado com o feed ao vivo ("Claude Code em acao").

Sem TTY (ex.: background) o painel se desliga e so escreve logs/live.feed.
"""
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from loguru import logger

LOG_DIR = Path(__file__).resolve().parent / "logs"
FEED_FILE = LOG_DIR / "live.feed"     # resumo (audit) — usado sem tty
WORK_FILE = LOG_DIR / "work.feed"     # detalhado: Claude Code PROGRAMANDO (aba do lado)
ERROR_LOG = LOG_DIR / "errors.log"

_BARS = " ▁▂▃▄▅▆▇█"
_DOT = {"ouvindo": "bright_green", "captei": "bright_yellow", "pensando": "yellow",
        "fazendo": "yellow", "falando": "bright_cyan", "erro": "bright_red", "mudo": "bright_red"}
# "humores" da mascote quando ociosa — cicla devagar (sensação de vida)
_MOODS = ["bright_cyan", "bright_green", "bright_magenta", "cyan",
          "spring_green1", "sky_blue1", "light_pink1", "pale_turquoise1"]


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _mascot(status: str, frame: int, muted: bool) -> list[str]:
    """Mascote BLOCO GRANDE (21 wide, 10 rows) — robô chunky, olhos/boca animados.
    Olhos e boca ocupam 2 linhas (mais alto/visível). Mudo = cara parada de verdade."""
    W = 19
    f = frame
    fr = lambda s: "█" + s[:W] + " " * (W - len(s[:W])) + "█"  # noqa: E731  linha com borda
    blink = (f % 38) < 2 and status != "erro" and not muted

    # --- olhos (centrados; 'pensando' faz vaguear de leve) ---
    if muted:                       # mudo: olhos "desligados" (traços), sem animação
        eye = "    ▬▬▬     ▬▬▬"
    elif status == "erro":
        eye = "    ███     ███"
    elif status == "captei":        # acordou — olhos arregalados, pulsam
        eye = ["  ██████   ██████", "   █████   █████", "    ████    ████"][(f // 3) % 3]
    elif blink:
        eye = "    ▀▀▀     ▀▀▀"
    elif status == "pensando":
        gap = [2, 4, 6, 4][(f // 3) % 4]
        eye = " " * gap + "███     ███"
    else:
        eye = "    ███     ███"

    # --- boca (centrada) ---
    if muted:
        mouth = "      ▄▄▄▄▄▄▄"
    elif status == "erro":
        mouth = "      ▀▀▀▀▀▀▀"
    elif status == "captei":        # sorriso empolgado — pisca entre dois estados
        mouth = ["     ▀▄████▄▀ ", "     ▀██████▀ "][(f // 4) % 2]
    elif status == "fazendo":       # mastigando (trabalhando)
        mouth = ["      ▄█▄█▄█▄", "      █▄█▄█▄█", "      ▄▄█▄█▄▄"][(f // 2) % 3]
    elif status == "falando":       # abrindo/fechando (falando de verdade)
        mouth = ["      ▄▄▄▄▄▄▄", "      ███████", "      ▄█████▄"][(f // 2) % 3]
    else:                           # ouvindo — sorrisinho
        mouth = "      ▀▄▄▄▄▄▀"

    blank = fr("")
    return [
        "▟" + "█" * W + "▙",
        blank,
        fr(eye), fr(eye),
        blank, blank,
        fr(mouth), fr(mouth),
        blank,
        "▜" + "█" * W + "▛",
    ]


class CallUI:
    def __init__(self, *, session_id, cwd, lang="pt", name="Claude", max_actions=6):
        self._cwd = cwd
        self._session_id = session_id
        self._lang = (lang or "pt")[:2]
        self.name = name
        self._heard = ""
        self._reply = ""
        self._error = ""
        self._hint = ""
        self._status = "ouvindo"
        self._actions: deque[str] = deque(maxlen=max_actions)
        self._frame = 0
        self._levels: deque = deque([0.0] * 256, maxlen=256)  # nível REAL do mic (wave)
        self._live = None
        self._anim_task = None
        self._console = None
        self.muted = False                  # mute: para de OUVIR (tecla M/espaco)
        self._typing = False                # modo escrever (tecla T)
        self._typing_buf = ""
        self.settings_open = False          # overlay de config (tecla C)
        self._settings = {"sens": 2, "sens_max": 4, "device": "default", "barge_in": False,
                          "stop_secs": 1.0, "noise": 1, "noise_max": 2, "code_effort": "xhigh"}
        self.enabled = sys.stdout.isatty()  # so desenha em terminal de verdade

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            FEED_FILE.write_text(f"# {self.name} — feed ao vivo ({_now()})\n")
            WORK_FILE.write_text(
                f"# {self.name} — Claude Code ao vivo ({_now()})\n"
                f"# o que o Claude esta fazendo de verdade: pensamento, tools, resultados\n\n")
        except OSError:
            pass

    # -------------------------------------------------- ciclo de vida
    def start(self):
        if not self.enabled:
            return
        try:
            from rich.console import Console
            from rich.live import Live
            self._console = Console()
            self._console.clear()  # limpa a tela toda ao iniciar
            self._live = Live(self._render(), console=self._console,
                              screen=True, refresh_per_second=15, transient=False)
            self._live.start()
            self._anim_task = asyncio.ensure_future(self._animate())
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[ui] painel off ({e})")
            self._live, self.enabled = None, False

    def stop(self):
        if self._anim_task:
            self._anim_task.cancel()
            self._anim_task = None
        if self._live:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

    async def _animate(self):
        try:
            while True:
                self._frame += 1
                self._refresh()
                await asyncio.sleep(0.07)
        except asyncio.CancelledError:
            pass

    def set_session(self, session_id: str):
        self._session_id = session_id

    def set_level(self, level: float):
        """Nível de áudio real do mic (0..1). Só armazena; o loop de animação desenha."""
        self._levels.append(max(0.0, min(1.0, level)))

    # -------------------------------------------------- eventos
    def heard(self, text: str):
        self._heard, self._reply, self._error, self._hint = text, "", "", ""
        self._actions.clear()
        self._status = "pensando"
        self._feed(f"[{_now()}] 🎤 ouvido: {text}")
        self._refresh()

    def tool(self, name: str, target: str | None = None):
        label = name if not target else f"{name} {target}"
        self._actions.append(label)
        self._status = "fazendo"
        self._feed(f"[{_now()}] 🔧 {label}")
        self._refresh()

    def reply_append(self, text: str):
        text = text.strip()
        if not text:
            return
        self._reply = (self._reply + " " + text).strip()
        self._status = "falando"
        self._feed(f"[{_now()}] 💬 {text}")
        self._refresh()

    def done(self):
        self._status = "erro" if self._error else "ouvindo"
        self._refresh()

    def status(self, msg: str):
        self._status = msg
        self._refresh()

    def hint(self, msg: str):
        self._hint = msg
        self._refresh()

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        try:
            from sounds import play
            play("off" if self.muted else "on")
        except Exception:  # noqa: BLE001
            pass
        self._feed(f"[{_now()}] {'🔇 MUTADO' if self.muted else '🎙️ ouvindo de novo'}")
        self._refresh()
        return self.muted

    def toggle_settings(self) -> bool:
        self.settings_open = not self.settings_open
        self._refresh()
        return self.settings_open

    def set_settings(self, **kw):
        self._settings.update(kw)
        self._refresh()

    def set_typing(self, on: bool, buf: str = ""):
        self._typing = on
        self._typing_buf = buf
        self._refresh()

    def error(self, msg: str, exc: BaseException | None = None):
        self._error = msg
        self._status = "erro"
        detail = msg
        if exc is not None:
            detail += "\n" + "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._log_error(detail)
        self._feed(f"[{_now()}] ❌ ERRO: {msg}")
        self._refresh()
        if not self.enabled:                       # sem painel: nao some o erro
            logger.error(f"[ui] {msg}")

    # -------------------------------------------------- work feed (Claude programando)
    def work_tool(self, name: str, inp: dict | None):
        """Tool real do Claude com input cheio -> work.feed (o que aparece na aba)."""
        inp = inp or {}
        out = [f"\n▶ {name}"]
        if name == "Edit":
            out.append(f"   {inp.get('file_path', '')}")
            for ln in str(inp.get("old_string", "")).splitlines()[:10]:
                out.append(f"   - {ln}")
            for ln in str(inp.get("new_string", "")).splitlines()[:10]:
                out.append(f"   + {ln}")
        elif name in ("Write", "NotebookEdit"):
            out.append(f"   {inp.get('file_path') or inp.get('notebook_path', '')}")
            body = inp.get("content") or inp.get("new_source") or ""
            for ln in str(body).splitlines()[:14]:
                out.append(f"   | {ln}")
        elif name in ("Bash", "BashOutput"):
            out.append(f"   $ {inp.get('command', '')}")
        elif name in ("Read", "Glob", "Grep", "WebFetch"):
            out.append(f"   {inp.get('file_path') or inp.get('path') or inp.get('pattern') or inp.get('url') or ''}")
        else:
            out.append("   " + json.dumps(inp, ensure_ascii=False)[:300])
        self._work("\n".join(out))

    def work_result(self, content, is_error=False):
        text = content if isinstance(content, str) else self._flatten(content)
        head = "   ✗ erro:" if is_error else "   ◀ resultado:"
        out = [head]
        for ln in str(text).splitlines()[:14]:
            out.append("     " + ln)
        self._work("\n".join(out))

    def work_think(self, text: str):
        text = " ".join((text or "").split())
        if text:
            self._work(f"\n💭 {text[:280]}")

    def work_text(self, text: str):
        text = " ".join((text or "").split())
        if text:
            self._work(f"\n🗣️  {text}")

    @staticmethod
    def _flatten(content) -> str:
        if isinstance(content, list):
            return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b)
                             for b in content)
        return str(content)

    def _work(self, text: str):
        try:
            with open(WORK_FILE, "a") as f:
                f.write(text + "\n")
        except OSError:
            pass

    # -------------------------------------------------- janela handoff (aba do lado)
    def open_window(self) -> bool:
        """Abre uma ABA no mesmo terminal mostrando o feed ao vivo. Bloqueia (osascript),
        entao chame via asyncio.to_thread. Retorna True se abriu."""
        if sys.platform != "darwin":
            self.error("aba só no macOS")
            return False
        feed = str(WORK_FILE)   # mostra o Claude PROGRAMANDO (tool/input/resultado), nao o resumo
        # sanitiza o que vai interpolado no osascript (nome/sessão com aspas quebrariam)
        safe_name = re.sub(r"""["'\\]""", "", self.name or "")
        safe_sid = re.sub(r"""["'\\]""", "", str(self._session_id or "?"))
        cmd = (f"clear; echo '{safe_name} — Claude Code ao vivo (programando)'; "
               f"echo '(sessao {safe_sid})'; echo; tail -n +1 -f '{feed}'")
        term = os.environ.get("TERM_PROGRAM", "")

        iterm = (f'tell application "iTerm"\n'
                 f'  if (count of windows) = 0 then\n'
                 f'    create window with default profile\n'
                 f'  else\n'
                 f'    tell current window to create tab with default profile\n'
                 f'  end if\n'
                 f'  tell current session of current window to write text "{cmd}"\n'
                 f'  activate\n'
                 f'end tell')
        term_tab = (f'tell application "Terminal" to activate\n'
                    f'delay 0.2\n'
                    f'tell application "System Events" to keystroke "t" using command down\n'
                    f'delay 0.3\n'
                    f'tell application "Terminal" to do script "{cmd}" in selected tab of front window')
        term_win = (f'tell application "Terminal" to do script "{cmd}"\n'
                    f'tell application "Terminal" to activate')

        attempts = ([("iTerm-tab", iterm)] if "iTerm" in term
                    else [("Terminal-tab", term_tab)])
        attempts.append(("Terminal-window", term_win))   # fallback sempre

        for label, script in attempts:
            try:
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    self._feed(f"[{_now()}] 🖥️  aba aberta ({label})")
                    return True
                self._log_error(f"open_window {label} rc={r.returncode}: {r.stderr.strip()}")
            except Exception as e:  # noqa: BLE001
                self._log_error(f"open_window {label} exc: {e!r}")
        self.error("não consegui abrir a aba (veja logs/errors.log)")
        return False

    # -------------------------------------------------- interno
    def _feed(self, line: str):
        try:
            with open(FEED_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _log_error(self, detail: str):
        try:
            with open(ERROR_LOG, "a") as f:
                f.write(f"\n===== {datetime.now().isoformat()} =====\n{detail}\n")
        except OSError:
            pass

    def _refresh(self):
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:  # noqa: BLE001
                pass

    def _size(self):
        c = self._console
        w = getattr(c, "width", 80) or 80
        h = getattr(c, "height", 24) or 24
        return w, h

    @staticmethod
    def _short(text: str, max_chars: int = 160) -> str:
        """Resume o que foi OUVIDO pra ~2 frases — só referência no painel (texto longo
        digitado/colado fica gigante). O texto cheio continua indo pro Claude e pro feed."""
        t = " ".join((text or "").split())
        if not t:
            return ""
        parts = re.split(r"(?<=[.!?…])\s+", t)
        s = " ".join(parts[:2]).strip()
        if len(s) > max_chars:
            s = s[:max_chars - 1].rstrip() + "…"
        return s

    def _wave(self, n: int, rows: int = 7) -> list[str]:
        """Wave SINCRONIZADA com o áudio real do mic (VU meter scrollando). Mais alta (7
        linhas), com ganho e uma textura leve de ruído pra não ficar reta/morta no silêncio."""
        cols = list(self._levels)[-n:]
        if len(cols) < n:
            cols = [0.0] * (n - len(cols)) + cols
        out = []
        for r in range(rows):  # r=0 topo
            line = []
            for i, v in enumerate(cols):
                shim = 0.05 * (0.5 + 0.5 * math.sin(self._frame * 0.4 + i * 0.6))  # ruído sutil
                vv = min(1.0, v * 1.35 + shim)
                level = int(vv * rows * 8) - (rows - 1 - r) * 8
                line.append(_BARS[max(0, min(8, level))])
            out.append("".join(line))
        return out

    def _render(self):
        from rich import box
        from rich.align import Align
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        working = self._status in ("pensando", "fazendo", "falando")
        # mudo NÃO esconde que ela está trabalhando: mantém face/wave/cor da atividade
        if self.muted and working:
            st_label = f"{self._status} (mudo)"
            color = _DOT.get(self._status, "white")
        elif self.muted:
            st_label, color = "mudo", _DOT["mudo"]
        else:
            st_label = self._status
            color = _DOT.get(self._status, "white")
        # mudo = mic fechado, NÃO "sem vida": se está TRABALHANDO (pensando/fazendo/falando)
        # a mascote anima normal (feedback de que tá rolando algo). Só fica estática/apagada
        # quando mudo E ociosa (nada acontecendo) — aí sim "cara de mudo".
        mascot_muted = self.muted and not working
        # borda PULSANDO (amarelo escuro<->claro) enquanto pensa/faz — "tá acontecendo algo"
        # borda PULSANDO DOURADO ao captar wake word — "te ouvi"
        border = color
        if self._status in ("pensando", "fazendo"):
            b = 0.40 + 0.60 * (0.5 + 0.5 * math.sin(self._frame * 0.30))  # ciclo ~1.5s
            v = max(0, min(255, int(b * 255)))
            border = f"#{v:02x}{v:02x}00"
        elif self._status == "captei":
            b = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._frame * 0.55))  # pulsa mais rápido
            v = max(0, min(255, int(b * 255)))
            border = f"#{v:02x}{int(v * 0.75):02x}00"   # dourado (r alto, g 75%, b=0)
        # cor da mascote: mudo = apagada (cinza, "desligada"); pulsa ao pensar/fazer;
        # captei = dourado brilhante; cicla "humores" quando ociosa; status nos outros
        if mascot_muted:
            mascot_color = "grey50"
        elif self._status in ("pensando", "fazendo"):
            mascot_color = border
        elif self._status == "captei":
            mascot_color = border
        elif self._status == "ouvindo" and not self.muted:
            mascot_color = _MOODS[(self._frame // 45) % len(_MOODS)]
        else:
            mascot_color = color
        w, h = self._size()
        wave_n = max(20, min(w - 12, 72))

        body = [Text("")]
        for ml in _mascot(self._status, self._frame, mascot_muted):
            body.append(Align.center(Text(ml, style=f"bold {mascot_color}")))
        body.append(Text(""))

        # topo: SEM repetir o nome (já está na moldura). Status e mudo um DEBAIXO do outro.
        body.append(Align.center(Text.assemble(("● ", color), (st_label, f"bold {color}"))))
        if self.muted:
            body.append(Align.center(Text("▸ MUDO   ·   M/espaço p/ voltar",
                                          style="bold bright_red")))
        body.append(Text(""))
        for wl in self._wave(wave_n):
            body.append(Align.center(Text(wl, style=color)))
        body.append(Text(""))

        info = [Text.assemble(("▸ ouvido   ", "bold cyan"),
                              (self._short(self._heard) or "—", "white"))]
        for a in self._actions:
            info.append(Text.assemble(("▸ ", "bold yellow"), (a, "yellow")))
        reply = self._reply or ("…" if self._status in ("pensando", "fazendo") else "—")
        info.append(Text.assemble(("▸ resposta ", "bold green"), (reply, "white")))
        if self._typing:
            info.append(Text.assemble(("▸ escrevendo ", "bold bright_magenta"),
                                      (self._typing_buf + "▏", "white"),
                                      ("   (Enter envia · Esc cancela · ⌘V cola)", "dim")))
        if self._hint and not self._reply:
            info.append(Text(f"▸ {self._hint}", style="dim yellow"))
        if self._error:
            info.append(Text.assemble(("▸ erro     ", "bold bright_red"),
                                      (self._error, "bright_red")))
        body.extend(info)

        if self.settings_open:
            body.append(Text(""))
            body.extend(self._settings_rows())

        sub = Text("C config · +/− sensib · ,/. espera · N ruído · E effort · V colar · "
                   "T escrever · M mutar · I interromper · R reset · Ctrl+C sair", style="dim")
        return Panel(Group(*body), box=box.HEAVY, border_style=border,
                     title=f"[bold]{self.name} — claude-call[/bold]", subtitle=sub,
                     height=h, padding=(1, 3))

    def _settings_rows(self):
        from rich.text import Text
        s = self._settings
        lvl = int(s.get("sens", 2))
        mx = int(s.get("sens_max", 4))
        bar = "▓" * (lvl + 1) + "░" * (mx - lvl)
        rows = [
            Text("▸ CONFIG  (C fecha)", style="bold magenta"),
            Text.assemble(("  ▸ sensibilidade mic  ", "bold"),
                          (f"[{bar}] {lvl}/{mx}", "bright_magenta"),
                          ("   + / −   (não ouve? aperta +)", "dim")),
            Text.assemble(("  ▸ espera p/ responder ", "bold"),
                          (f"{s.get('stop_secs', 1.0):.1f}s", "white"),
                          ("   , / .   (maior = não te atropela)", "dim")),
            Text.assemble(("  ▸ filtro de ruído    ", "bold"),
                          (["off", "médio", "alto"][int(s.get("noise", 1))], "white"),
                          ("   N   (ignora ruído curto)", "dim")),
            Text.assemble(("  ▸ effort programar   ", "bold"),
                          ("ultracode (max)" if s.get("code_effort") == "max" else "xhigh", "white"),
                          ("   E   (voz fica Sonnet/medium)", "dim")),
            Text.assemble(("  ▸ entrada (mic)      ", "bold"),
                          (str(s.get("device", "default")), "white"),
                          ("   (fixo: mic do Mac)", "dim")),
            Text.assemble(("  ▸ interromper fala   ", "bold"), ("tecla I (agora)", "white")),
            Text.assemble(("  ▸ barge-in por voz   ", "bold"),
                          ("ON" if s.get("barge_in") else "OFF",
                           "bright_green" if s.get("barge_in") else "dim"),
                          ("   default OFF · só com fone (CALL_ECHO_GATE=0 no .env)", "dim")),
            Text.assemble(("  ▸ reset padrão       ", "bold"),
                          ("tecla R", "white"),
                          ("   (VAD 2/4 · mic do computador · barge-in OFF)", "dim")),
        ]
        return rows
