"""Descobre a sessao mais recente do Claude Code para um diretorio.

O grande truque do claude-call: a chamada de voz nao e um assistente generico —
ela RETOMA a sua sessao viva do Claude Code (mesmas skills, MCP, memoria e contexto).
Cada projeto guarda seus transcripts em ~/.claude/projects/<cwd-encodado>/<id>.jsonl.
"""
import re
from pathlib import Path


def _encode_cwd(cwd: str) -> str:
    """Mesma codificacao que o Claude Code usa pro nome do diretorio do projeto."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def latest_session(cwd: str) -> str | None:
    """ID da conversa mais recente desse cwd (a 'sua sessao'), ou None."""
    proj = Path.home() / ".claude" / "projects" / _encode_cwd(cwd)
    if not proj.is_dir():
        return None
    transcripts = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return transcripts[0].stem if transcripts else None
