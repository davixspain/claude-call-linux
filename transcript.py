"""Transcript por call — cada ligacao salva em transcripts/<timestamp>.md, NUNCA sobrescrito.

Escrito incrementalmente conforme a call acontece (sobrevive a crash). Conversa pura:
suas falas + o que o assistente respondeu, com horario. Lista/abre com `claude-call transcripts`.
"""
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent / "transcripts"


class Transcript:
    def __init__(self, *, name: str = "Claude", session_id: str = "", lang: str = "pt", cwd: str = ""):
        self.path = None
        self._name = name
        self._turns = 0
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            self.path = DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.md"
            self.path.write_text(
                f"# Call · {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                f"- name: {name}\n- lang: {lang}\n- session: {session_id or '—'}\n- cwd: {cwd or '—'}\n\n---\n\n",
                encoding="utf-8")
        except OSError:
            self.path = None

    def _w(self, text: str):
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def user(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        self._turns += 1
        self._w(f"**Você** · {datetime.now().strftime('%H:%M:%S')}\n{text}\n\n")

    def assistant(self, text: str):
        text = " ".join((text or "").split()).strip()
        if not text:
            return
        self._w(f"**{self._name}** · {datetime.now().strftime('%H:%M:%S')}\n{text}\n\n")

    def close(self):
        if self.path and self._turns:
            self._w(f"---\n_fim · {self._turns} turnos · {datetime.now().strftime('%H:%M:%S')}_\n")
