"""Configuracao do claude-call (tudo via env CALL_*, com defaults sensatos)."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Carrega o .env do repo no import, pra QUALQUER entrypoint (call, doctor) ver a config.
load_dotenv(Path(__file__).resolve().parent / ".env")

# Audio
SAMPLE_RATE_IN = 16000    # Silero VAD so aceita 8k/16k; whisper tambem quer 16k
SAMPLE_RATE_OUT = 24000   # edge-tts

LANG = os.getenv("CALL_LANG", "en")

# Nome/persona do assistente (aparece no painel e na saudacao).
NAME = os.getenv("CALL_NAME", "Claude")

# Voz default por idioma (edge-tts). Sobrescreva com CALL_VOICE.
_DEFAULT_VOICE = {
    "en": "en-US-AndrewNeural",
    "pt": "pt-BR-AntonioNeural",
    "es": "es-ES-AlvaroNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "it": "it-IT-DiegoNeural",
    "ja": "ja-JP-KeitaNeural",
}
# TTS provider: edge (free, default) | elevenlabs | cartesia | openai | rime | deepgram
TTS = os.getenv("CALL_TTS", "edge").lower()
TTS_API_KEY = os.getenv("CALL_TTS_API_KEY") or None
TTS_MODEL = os.getenv("CALL_TTS_MODEL") or None

# Voice: for edge, fall back to a per-language default. For premium providers, leave
# it as set (a provider voice id) — the factory picks a sensible default if empty.
EDGE_VOICE = _DEFAULT_VOICE.get(LANG[:2], "en-US-AndrewNeural")  # free fallback voice
_raw_voice = os.getenv("CALL_VOICE") or None
if TTS == "edge":
    VOICE = _raw_voice or EDGE_VOICE
else:
    VOICE = _raw_voice
VOICE_RATE = os.getenv("CALL_VOICE_RATE", "+0%")

# Saudacao inicial por idioma
_GREETING = {
    "en": f"Hey, it's {NAME}. What's up?",
    "pt": f"Oi, aqui é o {NAME}. Pode falar.",
    "es": f"Hola, soy {NAME}. Dime.",
    "fr": f"Salut, c'est {NAME}. Je t'écoute.",
    "de": f"Hey, hier ist {NAME}. Was gibt's?",
    "it": f"Ciao, sono {NAME}. Dimmi.",
    "ja": f"もしもし、{NAME}です。どうぞ。",
}
GREETING = os.getenv("CALL_GREETING") or _GREETING.get(LANG[:2], _GREETING["en"])

# Cerebro (Claude Code). Vazio = herda o modelo padrao do seu Claude Code.
MODEL = os.getenv("CALL_MODEL") or None
EFFORT = os.getenv("CALL_EFFORT") or None             # low|medium|high|xhigh — voz: medium (rapido)
# Quando for PROGRAMAR (alterar codigo), troca pra um modelo/effort mais fortes.
CODE_MODEL = os.getenv("CALL_CODE_MODEL", "opus")     # default Opus 4.8 (alias 'opus')
CODE_EFFORT = os.getenv("CALL_CODE_EFFORT", "xhigh")
# Tetto anti-burn: max invocazioni del cervello (claude -p) per ora. Falsi trigger
# ripetuti del wake-word non devono poter bruciare credito agente senza freno.
MAX_PER_HOUR = int(os.getenv("CALL_MAX_PER_HOUR", "20"))
# Tetto duro alla lunghezza parlata per turno (caratteri). Senza fone non si puo'
# interrompere a voce (mic muto mentre parla) - il modello a volte ignora la regola
# "1-2 frasi" e legge elenchi interi. Questo taglia DAVVERO, non e' un suggerimento.
MAX_SPOKEN_CHARS = int(os.getenv("CALL_MAX_SPOKEN_CHARS", "260"))
# Permissoes do agente headless. Hands-free precisa nao travar em prompt.
# ATENCAO: skip-permissions deixa o agente rodar tools/bash sem perguntar. Veja o README.
PERMISSION = os.getenv("CALL_PERMISSION", "--dangerously-skip-permissions")

# Sessao: continua a SUA conversa mais recente desse diretorio (o diferencial).
CONTINUE = os.getenv("CALL_CONTINUE", "1") not in ("0", "false", "no")
SESSION_ID = os.getenv("CALL_SESSION_ID") or None
CWD = os.getenv("CALL_CWD") or os.getcwd()

# Ativacao: por padrao voce chama o assistente pelo NOME dele (CALL_NAME, ex.: "claude")
# pra ele ouvir/executar — sem o wake nao transcreve/executa (so loga gated). Mic sempre
# aberto (modo call) com CALL_WAKE=off (ou vazio/none/0). Multiplos: "claude,claudio".
_WAKE_RAW = os.getenv("CALL_WAKE", NAME.lower())
WAKE = ([] if _WAKE_RAW.strip().lower() in ("", "off", "none", "0", "false", "no")
        else [w.strip().lower() for w in _WAKE_RAW.split(",") if w.strip()])
ACTIVE_WINDOW = float(os.getenv("CALL_ACTIVE_WINDOW", "25"))

# Watchdog do turno: se o cérebro travar, recomeça sozinho (respawn FRESH) em vez de ficar
# preso em "pensando". FIRST_RESP = s sem NENHUMA resposta (resume pesado/daemon morto).
# TURN = s travado depois de já ter respondido (tool/stall longo; acima do timeout do Bash).
FIRST_RESP_TIMEOUT = float(os.getenv("CALL_FIRST_RESP_TIMEOUT", "75"))
TURN_TIMEOUT = float(os.getenv("CALL_TURN_TIMEOUT", "120"))
# Teto quando uma TOOL esta rodando (Bash/Task pode levar minutos sem emitir nada).
TOOL_TIMEOUT = float(os.getenv("CALL_TOOL_TIMEOUT", "600"))

# Anti-eco
ECHO_GATE = os.getenv("CALL_ECHO_GATE", "1") not in ("0", "false", "no")
ECHO_TAIL = float(os.getenv("CALL_ECHO_TAIL", "0.8"))  # segs que o mic fica mudo APOS ela falar
AEC = os.getenv("CALL_AEC", "0") not in ("0", "false", "no")

# VAD (deteccao de fala). min_volume MENOR = capta tua fala real (o mic do MacBook é
# baixo; com 0.6 o VAD rejeitava e nao transcrevia mesmo a wave mexendo). stop_secs MAIOR
# = espera vc TERMINAR (com 0.2 ela cortava no meio). Ajustavel ao vivo (+/-) e por env.
VAD_CONFIDENCE = float(os.getenv("CALL_VAD_CONFIDENCE", "0.5"))  # menor = ouve melhor (nivel 2)
VAD_START_SECS = float(os.getenv("CALL_VAD_START_SECS", "0.2"))  # "filtro de ruido": maior ignora blip curto
VAD_STOP_SECS = float(os.getenv("CALL_VAD_STOP_SECS", "1.0"))    # espera vc TERMINAR (anti-atropelo)
VAD_MIN_VOLUME = float(os.getenv("CALL_VAD_MIN_VOLUME", "0.2"))

# Dispositivo de entrada (mic) por NOME (os índices do pyaudio mudam quando conecta/desconecta
# device — por isso nunca por índice). Vazio = mic do computador.
INPUT_DEVICE_NAME = (os.getenv("CALL_INPUT_DEVICE") or "").strip() or None

# Feedback visual: painel ao vivo no terminal (ouvido/fazendo/resposta) + janela
# "Claude Code ao vivo" sob comando de voz. Auto-desliga sem TTY (ex.: background).
UI = os.getenv("CALL_UI", "1") not in ("0", "false", "no")

# Hotkey GLOBAL (sem foco no terminal): segura a tecla N segundos -> muta/desmuta.
# off/none/0/vazio = desliga e NAO importa pynput (~22MB de RAM a menos). O M no terminal
# continua mutando — só perde o atalho sem foco. Default f9.
_HOTKEY_RAW = os.getenv("CALL_HOTKEY", "f9").strip()
HOTKEY = None if _HOTKEY_RAW.lower() in ("", "off", "none", "0", "false", "no") else _HOTKEY_RAW
HOTKEY_SECS = float(os.getenv("CALL_HOTKEY_SECS", "3"))

# STT provider: local (whisper.cpp, gratis) | elevenlabs | groq | openai
STT = os.getenv("CALL_STT", "local").lower()
STT_API_KEY = os.getenv("CALL_STT_API_KEY") or None
STT_MODEL = os.getenv("CALL_STT_MODEL") or None

# STT local (whisper.cpp). expandvars: %USERPROFILE% (Windows) e $HOME funcionam no .env.
WHISPER_MODEL = os.path.expanduser(os.path.expandvars(os.getenv(
    "CALL_WHISPER_MODEL", str(Path.home() / ".cache" / "whisper" / "ggml-small.bin")
)))
WHISPER_PORT = int(os.getenv("CALL_WHISPER_PORT", "8099"))
USE_WHISPER_SERVER = os.getenv("CALL_WHISPER_SERVER", "1") not in ("0", "false", "no")

# Quanto tempo de SILENCIO antes da call se encerrar sozinha (segundos).
# "0"/"off" = nunca encerra por inatividade (so Ctrl+C ou "encerrar"). Default 30 min.
IDLE_TIMEOUT = os.getenv("CALL_IDLE_TIMEOUT", "1800")


# Regras de fala (system prompt apensado a cada turno). Mantem a resposta "de call".
_VOICE_RULES = {
    "en": (
        "You are on a live VOICE call — not coding, not writing a report. Talk like a "
        "real person on a phone call: relaxed, warm, casual, contracted. Keep it SHORT, "
        "1-2 sentences, the way it actually comes out in conversation. "
        "Do NOT narrate what you're doing internally — no 'let me edit the file', 'running "
        "the command', 'opening the code', 'checking the skill'. Nobody talks like that on a "
        "call; just deliver the result, naturally. "
        "NEVER read aloud: markdown, lists, code blocks, URLs, file paths, IDs, hashes, "
        "long numbers, or emojis. If you must hand over something written, summarize it in speech. "
        "No assistant filler ('how can I help', 'understood', 'certainly!'). If you don't "
        "know yet, say something natural like 'hold on, let me check that'. "
        "GOLDEN RULE: when in doubt, SIMPLIFY. Fewer words, more human."
    ),
    "pt": (
        "Voce ta numa ligacao de voz. Fale brasileiro mesmo: to, ta, ne, ce, pra, pro, numa. "
        "MAXIMO 1-2 frases curtas. So a CONCLUSAO, direto ao ponto: o que era, se resolveu, "
        "o que voce fez. Ex.: 'resolvido, era o timeout, ja arrumei' / 'nao era bug de "
        "controle, era cache; ajustei'. "
        "NUNCA fale nome de arquivo, caminho, codigo, URL, comando, lista, numero longo NEM EMOJI. "
        "Se precisar apontar algo, diz o nome da SESSAO ou da tarefa, nunca o arquivo. "
        "Nao narre passo a passo o que ta fazendo — so o resultado final. "
        "Se nao sabe ainda, fala 'perai'. Nada de 'claro!', 'certamente!', 'entendido!'. "
        "REGRA DE OURO: menos e mais. Direto."
    ),
    "it": (
        "Sei in chiamata vocale — non stai scrivendo codice ne un report. Parla come una "
        "persona vera al telefono: rilassato, diretto, frasi corte, italiano naturale. "
        "MASSIMO 1-2 frasi, la CONCLUSIONE: cos'era, se e risolto, cosa hai fatto. "
        "Non narrare i passaggi interni ('sto modificando il file', 'eseguo il comando', "
        "'controllo lo skill') — nessuno parla cosi al telefono, dai solo il risultato. "
        "NON leggere MAI ad alta voce: markdown, liste, blocchi di codice, URL, percorsi "
        "file, ID, hash, numeri lunghi, emoji. Se devi consegnare qualcosa di scritto, "
        "riassumilo a voce. Niente filler da assistente ('come posso aiutarti', 'certamente!', "
        "'capito!'). Se non sai ancora, di' qualcosa di naturale tipo 'aspetta, controllo'. "
        "Se ti chiede 'cosa sai fare' o 'che strumenti hai': NON elencare tool/skill/agenti "
        "uno per uno, rispondi con UNA frase tipo 'tante cose, dimmi il task e vediamo'. "
        "Non puoi essere interrotto a voce mentre parli (niente cuffie): per questo sei "
        "ANCORA piu' breve del normale, mai un elenco, mai piu' di 2 frasi. "
        "REGOLA D'ORO: nel dubbio, SEMPLIFICA. Meno parole, piu umano."
    ),
}
VOICE_RULES = os.getenv("CALL_SYSTEM") or _VOICE_RULES.get(LANG[:2], _VOICE_RULES["en"])
WHISPER_LANG = LANG[:2]
