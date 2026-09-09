"""Smoke: os módulos de runtime IMPORTAM em todos os SOs (o CI roda em
ubuntu/macos/windows — pega import quebrado tipo termios/winsound fora de guard).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_import_runtime_modules():
    import call        # noqa: F401 — pipeline, VAD, transports
    import controls    # noqa: F401
    import doctor      # noqa: F401
    import sounds      # noqa: F401
    import ui          # noqa: F401


def test_setup_keys_no_tty_returns_none():
    # no CI o stdin nunca é TTY: tem que sair limpo ANTES de tocar termios/msvcrt
    import call
    assert call._setup_keys(lambda s: None, None) is None


def test_sounds_play_never_raises(monkeypatch):
    import sounds
    for kind in ("heard", "wake", "on", "off", "done", "desconhecido"):
        sounds.play(kind)   # não pode levantar em NENHUM SO (CI roda nos 3)


def test_whisper_model_expands_env_vars(monkeypatch):
    # O README Windows manda setar CALL_WHISPER_MODEL=%USERPROFILE%\... no .env —
    # tem que expandir de verdade (no CI Windows isso roda com %VAR% nativo).
    import importlib
    import os
    if os.name == "nt":
        monkeypatch.setenv("CALL_WHISPER_MODEL", r"%USERPROFILE%\.cache\whisper\x.bin")
    else:
        monkeypatch.setenv("CALL_WHISPER_MODEL", "$HOME/.cache/whisper/x.bin")
    import config
    importlib.reload(config)
    assert "%" not in config.WHISPER_MODEL
    assert "$" not in config.WHISPER_MODEL
