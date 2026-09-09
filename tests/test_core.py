"""Testes das funções PURAS do claude-call (sem áudio, sem rede, sem daemon).

Rode com:  uv run pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import ClaudeBrain, _clean_for_speech, _fuzzy_wake  # noqa: E402


def _brain(**kw):
    kw.setdefault("voice_rules", "x")
    return ClaudeBrain(**kw)


# ---------------------------------------------------------------- fala/limpeza

def test_clean_for_speech_markdown():
    assert _clean_for_speech("**bold** and `code`") == "bold and code"
    assert _clean_for_speech("[link](http://x)") == "link"
    assert _clean_for_speech("# title") == "title"


def test_clean_for_speech_emoji_and_punct_only():
    assert _clean_for_speech("🚀✅") == ""
    assert _clean_for_speech("--") == ""
    assert _clean_for_speech("...") == ""
    assert _clean_for_speech("  |  ") == ""
    assert _clean_for_speech("ok! 🚀") == "ok!"


def test_clean_for_speech_keeps_text():
    assert _clean_for_speech("Olá, tudo bem?") == "Olá, tudo bem?"
    assert _clean_for_speech("123") == "123"


# ---------------------------------------------------------------- wake word

def test_fuzzy_wake_positive():
    assert _fuzzy_wake("claud", "claude")
    assert _fuzzy_wake("audinha", "claudinha")


def test_fuzzy_wake_negative():
    assert not _fuzzy_wake("fraude", "claude")
    assert not _fuzzy_wake("galinha", "claudinha")
    assert not _fuzzy_wake("ok", "claude")       # curto demais
    assert not _fuzzy_wake("", "claude")


def test_strip_wake():
    b = _brain(wake_words=["claude"])
    assert b._strip_wake("claude, roda os testes") == "roda os testes"
    assert b._strip_wake("claude") == ""


def test_should_answer_gates_without_wake():
    b = _brain(wake_words=["claude"])
    assert b._should_answer("claude, oi")
    assert not b._should_answer("conversa de fundo qualquer")
    b2 = _brain(wake_words=[])                    # open mic: responde tudo
    assert b2._should_answer("qualquer coisa")


# ---------------------------------------------------------------- frases (drain)

def test_drain_splits_sentences():
    b = _brain()
    rest, sents = b._drain("First one. Second one! And the tail")
    assert sents == ["First one.", "Second one!"]
    assert rest == " And the tail"


def test_drain_newline_boundary():
    b = _brain()
    rest, sents = b._drain("line one\nline two")
    assert sents == ["line one"]
    assert rest == "line two"


# ---------------------------------------------------------------- watchdog

def test_watch_decision_awaiting():
    b = _brain(first_resp_timeout=45, stall_timeout=120)
    assert b._watch_decision(True, 5)[0] == "none"
    assert b._watch_decision(True, 13)[0] == "hint"
    assert b._watch_decision(True, 46)[0] == "recover"


def test_watch_decision_stall_vs_tool():
    b = _brain(first_resp_timeout=45, stall_timeout=120, tool_timeout=600)
    assert b._watch_decision(False, 130)[0] == "recover"           # stall normal
    assert b._watch_decision(False, 130, in_tool=True)[0] == "none"  # build legítimo
    assert b._watch_decision(False, 601, in_tool=True)[0] == "recover"


# ---------------------------------------------------------------- intents i18n

def test_intent_en_mutate_reaches_code_mode():
    b = _brain(lang="en")
    assert b._desired_mode("fix the bug in the parser") == "code"
    assert b._desired_mode("implement the new endpoint") == "code"
    assert b._desired_mode("refactor this module") == "code"
    assert b._desired_mode("deploy it") == "code"


def test_intent_en_readonly_stays_chat():
    b = _brain(lang="en")
    assert b._desired_mode("explain what this function does") == "chat"
    assert b._desired_mode("just take a look, don't change anything") == "chat"


def test_intent_pt_still_works():
    b = _brain(lang="pt")
    assert b._desired_mode("conserta o bug do parser") == "code"
    assert b._desired_mode("só analisa, não mexe em nada") == "chat"
    assert b._desired_mode("por que isso quebrou?") == "chat"


def test_intent_pt_speaker_using_english_verbs():
    b = _brain(lang="pt")                          # en sempre incluso
    assert b._desired_mode("faz o deploy") == "code"
    assert b._desired_mode("fix the parser") == "code"


def test_show_re_per_lang():
    en = _brain(lang="en")
    pt = _brain(lang="pt")
    assert en._show_re.search("show me the terminal")
    assert pt._show_re.search("mostra a tela do claude")


# ---------------------------------------------------------------- anti-eco

def test_is_echo_catches_own_speech():
    b = _brain()
    b._remember_speech("resolvido, era o timeout do cache")
    assert b._is_echo("era o timeout do cache")
    assert not b._is_echo("roda os testes de novo")


