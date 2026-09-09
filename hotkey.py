"""Hotkey GLOBAL (funciona sem foco no terminal) pra mutar/desmutar.

Segura a tecla (default F9) por ~3s -> dispara o toggle. Usa pynput (precisa de
permissão de Acessibilidade no macOS na 1a vez). Se faltar pynput/permissão, desliga
sozinho (o M no terminal continua valendo).
"""
import threading

from loguru import logger


def _resolve(keyboard, name: str):
    name = (name or "f9").strip().lower()
    if hasattr(keyboard.Key, name):           # f9, f8, cmd_r, alt_r, etc.
        return getattr(keyboard.Key, name)
    return keyboard.KeyCode.from_char(name[:1])   # uma letra


def _match(k, target, keyboard) -> bool:
    try:
        if isinstance(target, keyboard.Key):
            return k == target
        return getattr(k, "char", None) == getattr(target, "char", None)
    except Exception:  # noqa: BLE001
        return False


def start_global_hotkey(key_name: str, hold_secs: float, on_toggle):
    """Inicia o listener global. Retorna o Listener (ou None se indisponível)."""
    try:
        from pynput import keyboard
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[hotkey] pynput indisponível ({e}); hotkey global off")
        return None
    target = _resolve(keyboard, key_name)
    st = {"timer": None}

    def _fire():
        st["timer"] = None
        try:
            on_toggle()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[hotkey] toggle: {e}")

    def on_press(k):
        if _match(k, target, keyboard) and st["timer"] is None:
            t = threading.Timer(hold_secs, _fire)
            t.daemon = True
            t.start()
            st["timer"] = t

    def on_release(k):
        if _match(k, target, keyboard) and st["timer"] is not None:
            st["timer"].cancel()
            st["timer"] = None

    try:
        lis = keyboard.Listener(on_press=on_press, on_release=on_release)
        lis.daemon = True
        lis.start()
        logger.info(f"[hotkey] global ON — segure '{key_name}' {hold_secs:.0f}s p/ mutar "
                    "(precisa permissão de Acessibilidade no macOS)")
        return lis
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[hotkey] não iniciou ({e})")
        return None
