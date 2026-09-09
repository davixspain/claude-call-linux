"""ClaudeBrain — o cerebro da call é o PROPRIO Claude Code, como daemon persistente.

Em vez de spawnar um `claude -p` novo a cada fala (cold, recarrega o contexto toda vez),
sobe UM processo `claude` em modo stream-json que fica vivo a call inteira:

  - o mic "conecta" nele escrevendo cada fala no stdin (mensagens JSON)
  - ele streama a resposta no stdout (falamos frase a frase)
  - contexto + cache ficam QUENTES entre os turnos (rapido depois do 1o)
  - com --resume <sua sessao>, o daemon É a sua conversa do Claude Code

Honestidade: nem um daemon "pensa" entre as falas — cada fala dispara uma inferencia
(o modelo roda nos servidores da Anthropic). O daemon mantem a sessao quente e viva;
nao e uma consciencia continua. Mas e um processo unico que o mic alimenta.
"""
import asyncio
import json
import math
import os
import re
import signal
import subprocess
import time
import uuid
from collections import deque

from loguru import logger

from pipecat.frames.frames import (
    Frame, TranscriptionFrame, TTSSpeakFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

_SENT_BOUNDARY = re.compile(r"[.!?…](?=\s)|\n")

# Emojis e simbolos pictograficos — TTS nunca deve ler isso (😀 ✅ 🎉 → ⚠).
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # emoticons, pictographs, transport, symbols ext
    "\U00002600-\U000027BF"   # misc symbols + dingbats (✅ ⚠ ✨ ✓ ...)
    "\U0001F1E6-\U0001F1FF"   # bandeiras
    "\U00002190-\U000021FF"   # setas
    "\U00002300-\U000023FF"   # misc tecnico (⏰ ⌨ ...)
    "\U00002B00-\U00002BFF"   # setas/simbolos diversos (⭐ ...)
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def _norm_words(s: str) -> list[str]:
    return [w for w in re.split(r"\W+", (s or "").lower()) if w]

# Palavra curta = só INTERROMPE a fala (nao vira prompt). Depois você continua falando.
_INTERRUPT_RE = re.compile(
    r"^\s*(ei+|ai+|opa|para|pára|parou|pera+|peraí|espera|calma|chega|stop|wait|hey)[\s!.,…]*$",
    re.IGNORECASE,
)

# Intenções POR IDIOMA (en sempre incluso — brasileiro fala 'deploy', 'commit'...).
# MUTAÇÃO real (vai MEXER no código/sistema) -> só ISSO paga o modo code. Ambíguos
# ('debug/bug/fix-me-explica', 'analisa') ficam de fora: quase sempre é só ENTENDER.
_MUTATE_P = {
    "en": r"\b(implement|refactor|rewrite|fix|patch|repair|edit|modify|replace|rename|"
          r"delete|remove|create\s+(a\s+|the\s+|an\s+)?(file|function|component|script|"
          r"class|method|test)|write\s+(the|a|some)\s+(file|function|code|test|script)|"
          r"deploy|commit|merge|install|uninstall|build\s+(it|the)|run\s+(the|it|that)|"
          r"execute)\b",
    "pt": r"\b(implementa|refator|conserta|corrig|edita|altera(r)?|substitu|cria(r)?\s+"
          r"(o\s+|um\s+|uma\s+)?(arquivo|fun[çc]|componente|script|classe|m[ée]todo|teste)|"
          r"escrev[ae]\s+o|comita|builda|instala|apaga|remov|delet|renomeia|"
          r"roda\s+o|executa\s+o)\b",
}

# "só pra entender, NÃO executa nada" -> NUNCA entra em code (read-only puro).
_NO_EXEC_P = {
    "en": r"\b(just\s+(to\s+)?(look|check|see|understand|analy[sz]e|explain)|"
          r"don'?t\s+(run|execute|change|modify|edit|touch|write)|"
          r"do\s+not\s+(run|execute|change|modify|edit|touch)|"
          r"without\s+(running|executing|changing|modifying|editing)|read[- ]?only)\b",
    "pt": r"\b(s[óo]\s+(pra|para)\s+(ver|entender|analis|olhar)|s[óo]\s+analis|apenas\s+analis|"
          r"n[ãa]o\s+(executa|mexe|mex|altera|muda|edita|faz)|sem\s+(executar|mexer|alterar|"
          r"mudar|editar|mexe))\b",
}

# Investigar/entender (read-only) -> fica no modo ATUAL (rápido), sem flip pro code.
_ANALYZE_P = {
    "en": r"\b(analy[sz]e|understand|explain|diagnos|investigat|review|assess|evaluate|"
          r"why\b|what\s+(is|does|happened)|take\s+a\s+look|look\s+(at|into)|"
          r"check\s+(the|if|why|what))\b",
    "pt": r"\b(analis|entend|explica|explique|diagnostic|investig|revis|avalia|"
          r"por\s*qu[eê]|o\s+que\s+|d[áa]\s+uma\s+olhada|olha\s+(o|no|a)|v[êe]\s+(o|se|por))\b",
}

# Comando de voz pra abrir a janela "Claude Code ao vivo".
_SHOW_P = {
    "en": r"\b(show|open)\b.*\b(screen|terminal|window|tab|claude|working|doing)\b",
    "pt": r"\b(mostra(r)?|abre|abrir|abra)\b.*\b(tela|terminal|janela|claude|fazendo|acontec|trabalh)",
}


def _intent_res(lang: str):
    """Compila os regexes de intenção pro idioma: en sempre + o idioma da call."""
    L = (lang or "en")[:2]

    def mk(pats: dict) -> re.Pattern:
        parts = [pats["en"]] + ([pats[L]] if L in pats and L != "en" else [])
        return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)

    return mk(_MUTATE_P), mk(_NO_EXEC_P), mk(_ANALYZE_P), mk(_SHOW_P)

# Campos de input de tool que viram "alvo" legivel no painel.
_TOOL_TARGET_FIELDS = ("file_path", "path", "notebook_path", "command",
                       "pattern", "query", "url", "prompt")


def _fuzzy_wake(word: str, wake: str) -> bool:
    """True se 'word' é provável mishear da wake word (sem deps externas).

    Cobre:
      - substring direto:  "claud"  ⊆ "claude"  ✓
      - sufixo fonético:   termina como o fim da wake (wake[-min_suf:])  ✓
    Rejeita palavras comuns que só rimam (ex.: "fraude" p/ "claude") → False
    """
    w = word.strip(",.!?:;-—").lower()
    if not w or len(w) < 4:
        return False
    if w in wake and len(w) >= len(wake) * 0.60:
        return True
    min_suf = max(4, math.ceil(len(w) * 0.75))
    return len(wake) >= min_suf and w.endswith(wake[-min_suf:])


def _tool_target(inp: dict) -> str | None:
    if not isinstance(inp, dict):
        return None
    for k in _TOOL_TARGET_FIELDS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            v = v.strip().replace("\n", " ")
            return v if len(v) <= 48 else v[:45] + "…"
    return None


def _clean_for_speech(s: str) -> str:
    """Tira markdown E emojis antes de falar — TTS nao deve ler *, #, `, links, 😀, ✅."""
    s = s.replace("```", " ")
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"[*_~#>|]+", "", s)
    s = re.sub(r"^\s*[-•·]\s*", "", s)
    s = _EMOJI.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Fragmentos so de pontuacao ("--", "...", "|") fazem o edge-tts retornar "no audio"
    # e engasgam a fala — pula se nao sobrou nada falavel (sem letra/numero).
    return s if re.search(r"[^\W_]", s) else ""


class ClaudeBrain(FrameProcessor):
    def __init__(
        self,
        *,
        voice_rules: str,
        model: str | None = None,
        effort: str | None = None,
        code_model: str | None = None,
        code_effort: str | None = None,
        permission_flag: str = "--dangerously-skip-permissions",
        wake_words: list[str] | None = None,
        active_window_secs: float = 25.0,
        fillers: list[str] | None = None,
        session_id: str | None = None,
        cwd: str | None = None,
        first_resp_timeout: float = 45.0,
        stall_timeout: float = 120.0,
        tool_timeout: float = 600.0,
        recover_phrase: str = "Sorry, I got stuck — please say that again.",
        lang: str = "en",
        ui=None,
        transcript=None,
    ):
        super().__init__()
        self._ui = ui
        self._transcript = transcript    # grava a conversa por call (transcripts/<data>.md)
        self._turn_reply = ""            # acumula a resposta falada do turno atual
        self._voice_rules = voice_rules
        self._cwd = cwd or os.getcwd()
        # Dois modos: chat (voz, rapido) e code (programar, forte). Troca on-the-fly.
        self._chat_model, self._chat_effort = model, effort
        self._code_model, self._code_effort = code_model, code_effort
        self._model, self._effort = model, effort
        self._mode = "chat"
        self._permission_flag = permission_flag
        self._wake = [w.lower() for w in (wake_words or []) if w.strip()]
        self._active_window = active_window_secs
        self._fillers = fillers or ["One sec.", "Hold on.", "Let me check.", "Give me a moment."]
        self._filler_i = 0

        self._resume = session_id          # se setado, RETOMA essa conversa
        self._session_id = session_id or str(uuid.uuid4())

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._buf = ""
        self._discard = False
        self._narrated_tool = False
        self._spoke = False
        self._active_until = 0.0
        self._awaiting = False   # esperando 1a resposta do turno (pro watchdog)
        # Watchdog do turno: recupera sozinho se o cérebro travar (ex.: --resume de sessão
        # pesada = prefill gigante que nunca volta). Mata + respawna FRESH + avisa.
        self._first_resp_timeout = first_resp_timeout  # s sem NENHUMA resposta -> recomeça
        self._stall_timeout = stall_timeout            # s travado no meio (já respondeu) -> recomeça
        self._recover_phrase = recover_phrase
        self._turn_seq = 0           # id do turno atual (invalida watchdog de turno antigo)
        self._turn_active = False    # há turno em andamento (entre _send e o 'result')
        self._last_activity = 0.0    # monotonic do último evento vindo do claude
        self._ignore_utterance = False   # mute decidido no INÍCIO da fala (deixa a atual terminar)
        self._recent_speech: deque = deque(maxlen=30)  # (t, set(palavras)) — anti-eco
        self._last_text = ""             # último pedido do usuário (pra refazer sozinho no recover)
        self._retried = False            # já refez este turno? (evita loop recover->refaz->recover)

        # Intenções no idioma da call (en sempre incluso).
        self._mutate_re, self._no_exec_re, self._analyze_re, self._show_re = _intent_res(lang)

        # Protocolo de controle stream-json (interrupt/set_model SEM matar o daemon).
        self._ctrl_id = 0
        self._ctrl_waiters: dict[str, asyncio.Future] = {}
        self._default_model: str | None = None   # modelo real do daemon quando chat_model=None

        # Watchdog ciente de tool longa: Bash/Task do claude pode rodar minutos sem emitir
        # NADA — não é stall. Enquanto uma tool executa, o teto vira tool_timeout.
        self._tool_timeout = tool_timeout
        self._in_tool = False

        # Latência por turno (log no result): stt / ttft / total.
        self._t_vadstop = 0.0
        self._t_send = 0.0
        self._t_first = 0.0
        self._last_stt_ms: float | None = None
        self._beeped = False             # 'captei' já tocou no fim da fala (VAD)?

        # Anti-burn: falsi trigger do wake-word repetidos não podem chamar o cerebro
        # (claude -p) sem freio — janela deslizante de 1h, teto em config.MAX_PER_HOUR.
        import config as _C
        self._max_per_hour = _C.MAX_PER_HOUR
        self._invoke_times: deque = deque()
        self._max_spoken_chars = _C.MAX_SPOKEN_CHARS
        self._cut_off = False    # ja' cortou a fala deste turno por excesso?

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        while self._invoke_times and now - self._invoke_times[0] > 3600:
            self._invoke_times.popleft()
        if len(self._invoke_times) >= self._max_per_hour:
            return True
        self._invoke_times.append(now)
        return False

    def _should_answer(self, text: str) -> bool:
        if not self._wake:
            return True
        if time.monotonic() < self._active_until:
            return True
        low = text.lower()
        words = re.split(r"\W+", low)
        return any(
            (w in low) or any(_fuzzy_wake(word, w) for word in words)
            for w in self._wake
        )

    def _has_wake(self, text: str) -> bool:
        low = text.lower()
        words = re.split(r"\W+", low)
        return any(
            (w in low) or any(_fuzzy_wake(word, w) for word in words)
            for w in self._wake
        )

    def _strip_wake(self, text: str) -> str:
        """Tira wake word e misheards ('audinha', 'odinha'…) do texto — são só gatilho."""
        if not self._wake:
            return text
        out = text
        for wake in self._wake:
            # exact substring removal
            low = out.lower()
            idx = low.find(wake)
            while idx != -1:
                out = out[:idx] + " " + out[idx + len(wake):]
                low = out.lower()
                idx = low.find(wake)
            # fuzzy word removal (misheards)
            parts = re.split(r"(\W+)", out)
            out = "".join(
                " " if (i % 2 == 0 and _fuzzy_wake(parts[i], wake)) else parts[i]
                for i, _ in enumerate(parts)
            )
        return re.sub(r"\s+", " ", out).strip(" ,.!?:;-—").strip()

    def _build_cmd(self) -> list[str]:
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
        ]
        cmd += ["--resume", self._session_id] if self._resume else ["--session-id", self._session_id]
        if self._permission_flag:
            cmd += [self._permission_flag]
        if self._model:
            cmd += ["--model", self._model]
        if self._effort:
            cmd += ["--effort", self._effort]
        cmd += ["--append-system-prompt", self._voice_rules]
        return cmd

    async def _ensure_proc(self):
        if self._proc and self._proc.returncode is None:
            return
        cmd = self._build_cmd()
        logger.info(f"[brain] up ({'resume '+self._session_id if self._resume else 'new session'})")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=2**20, env={**os.environ},
        )
        self._reader = self.create_task(self._read_loop())
        self.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        try:
            async for raw in self._proc.stderr:
                line = raw.decode(errors="ignore").rstrip()
                if line:
                    logger.debug(f"[claude] {line}")
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, text: str, *, is_retry: bool = False):
        if not is_retry:
            self._last_text = text   # guarda pro recover refazer; retry não sobrescreve nem rearma
            self._retried = False
        await self._ensure_proc()
        if not self._beeped:    # bip: captei sua fala (se já não beepou no fim da fala)
            from sounds import play
            play("heard")
        self._beeped = False
        if self._ui:
            self._ui.heard(text)
        if self._transcript:
            self._transcript.user(text)
        self._turn_reply = ""
        self._buf = ""
        self._discard = False
        self._narrated_tool = False
        self._spoke = False
        self._cut_off = False
        self._awaiting = True
        self._turn_active = True
        self._turn_seq += 1
        self._t_send = time.monotonic()   # métricas do turno (ttft/total no result)
        self._t_first = 0.0
        self._last_activity = time.monotonic()
        self.create_task(self._slow_watch(self._turn_seq))
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        payload = (json.dumps(msg) + "\n").encode()
        try:
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            logger.error("[brain] daemon dropped; restarting")
            self._proc = None
            await self._ensure_proc()
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()

    async def _control(self, subtype: str, *, timeout: float = 5.0, **fields) -> bool:
        """Manda um control_request pro daemon (protocolo stream-json) e espera a resposta.
        True = success. É assim que interrompemos o turno e trocamos de modelo SEM matar
        o processo (validado no CLI 2.1.200: interrupt/set_model respondem em ~0s)."""
        if not (self._proc and self._proc.returncode is None and self._proc.stdin):
            return False
        self._ctrl_id += 1
        rid = f"cc_{self._ctrl_id}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ctrl_waiters[rid] = fut
        msg = {"type": "control_request", "request_id": rid,
               "request": {"subtype": subtype, **fields}}
        try:
            self._proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self._proc.stdin.drain()
            return bool(await asyncio.wait_for(fut, timeout))
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError, OSError):
            logger.debug(f"[brain] control_request {subtype} sem resposta (CLI antigo?)")
            return False
        finally:
            self._ctrl_waiters.pop(rid, None)

    async def interrupt_turn(self):
        """Para o turno atual DE VERDADE — geração E tools — não só a fala. Sem isso,
        'para!' silenciava o TTS mas o daemon continuava editando/cobrando tokens."""
        self._discard = True
        self._buf = ""
        if not self._turn_active:
            return
        await self._control("interrupt", timeout=3.0)
        # espera o result do turno interrompido fechar (evita corrida com o próximo _send)
        for _ in range(20):
            if not self._turn_active:
                break
            await asyncio.sleep(0.1)

    async def _iter_lines(self):
        """Le o stdout em chunks e quebra por '\\n' na mao. O `async for ... in stdout`
        usa readline (limite de 1MB/linha) e estoura LimitOverrunError quando o claude
        emite uma linha JSON gigante (tool/conteudo grande). read() nao tem esse limite."""
        buf = b""
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:
                if buf.strip():
                    yield buf.decode(errors="ignore").strip()
                break
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                raw, buf = buf[:nl], buf[nl + 1:]
                line = raw.decode(errors="ignore").strip()
                if line:
                    yield line

    async def _read_loop(self):
        try:
            async for line in self._iter_lines():
                self._last_activity = time.monotonic()   # sinal de vida p/ o watchdog
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    await self._handle_event(ev)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — UM evento estranho não pode matar o loop
                    logger.exception(f"[brain] evento malformado ignorado: {line[:200]}")
            # stdout fechou: daemon saiu. Se foi NO MEIO de um turno, não deixa travado —
            # zera o relógio do watchdog pra ele recuperar (respawn fresh) já no próximo tick.
            if self._turn_active:
                logger.warning("[brain] daemon saiu sem 'result' no meio do turno")
                self._last_activity = 0.0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("[brain] read loop died")
            self._last_activity = 0.0    # deixa o watchdog recuperar
            if self._ui:
                self._ui.error(f"cérebro caiu: {e}", e)

    async def _handle_event(self, ev: dict):
        """Despacha UM evento do daemon (stream-json). Erros aqui não matam o read loop."""
        etype = ev.get("type")

        if etype == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                self._session_id = sid
                self._resume = sid
                if self._ui:
                    self._ui.set_session(sid)
            return

        # resposta de control_request (interrupt/set_model) -> acorda quem espera
        if etype == "control_response":
            resp = ev.get("response") or {}
            fut = self._ctrl_waiters.get(resp.get("request_id", ""))
            if fut and not fut.done():
                fut.set_result(resp.get("subtype") == "success")
            return

        # deltas: latencia baixa pra VOZ (fala frase a frase) + filler ao usar tool
        if etype == "stream_event":
            if self._awaiting:       # 1a resposta do turno: TTFT + desarma watchdog
                self._awaiting = False
                self._t_first = time.monotonic()
            inner = ev.get("event", {})
            itype = inner.get("type")
            if itype == "content_block_start":
                if inner.get("content_block", {}).get("type") == "tool_use" \
                   and not self._narrated_tool and not self._discard:
                    self._narrated_tool = True
                    await self._say(self._next_filler())
            elif itype == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta" and not self._discard and not self._cut_off:
                    self._buf += delta.get("text", "")
                    self._buf, sents = self._drain(self._buf)
                    for s in sents:
                        if len(self._turn_reply) >= self._max_spoken_chars:
                            # tetto duro: senza fone non si interrompe a voce, quindi
                            # taglio DAVVERO qui — smette pure di generare (risparmia).
                            self._cut_off = True
                            logger.warning(
                                f"[brain] risposta tagliata a {self._max_spoken_chars} caratteri "
                                "(no headphones, no barge-in)")
                            self.create_task(self.interrupt_turn())
                            break
                        if await self._say(s):
                            self._spoke = True
                            self._turn_reply += s + " "
                            if self._ui:
                                self._ui.reply_append(s)

        # mensagem COMPLETA do assistente: tool_use (input cheio) + thinking + texto
        # -> alimenta o painel (compacto) e o work.feed (Claude Code programando)
        elif etype == "assistant":
            # modelo REAL do daemon (o init mente quando --model é passado):
            # captura pro set_model de volta quando chat_model é None (default).
            m = (ev.get("message") or {}).get("model")
            if m and self._mode == "chat" and not self._chat_model:
                self._default_model = m
            content = (ev.get("message") or {}).get("content")
            for b in (content if isinstance(content, list) else []):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    self._in_tool = True   # tool rodando: watchdog usa teto de tool
                    name, inp = b.get("name") or "tool", b.get("input") or {}
                    if self._ui:
                        self._ui.tool(name, _tool_target(inp))
                        self._ui.work_tool(name, inp)
                elif bt == "thinking" and self._ui:
                    self._ui.work_think(b.get("thinking", ""))
                elif bt == "text" and self._ui:
                    self._ui.work_text(b.get("text", ""))

        # resultado de tool (stdout/erro) -> work.feed. CUIDADO: content pode ser
        # STRING (ex.: o aviso local-command-stdout que o set_model emite) — só
        # lista de blocos interessa aqui.
        elif etype == "user":
            content = (ev.get("message") or {}).get("content")
            for b in (content if isinstance(content, list) else []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    self._in_tool = False  # tool terminou
                    if self._ui:
                        self._ui.work_result(b.get("content"), b.get("is_error"))

        elif etype == "result":
            self._turn_active = False     # turno fechou: desarma o watchdog
            self._awaiting = False
            self._in_tool = False
            now = time.monotonic()
            stt = f"{self._last_stt_ms:.0f}ms" if self._last_stt_ms else "–"
            ttft = f"{(self._t_first - self._t_send) * 1000:.0f}ms" \
                if self._t_first > self._t_send else "–"
            logger.info(f"[turn] stt={stt} ttft={ttft} "
                        f"total={now - self._t_send:.1f}s")
            self._last_stt_ms = None
            tail = self._buf.strip()
            if not self._discard and not self._cut_off and await self._say(self._buf):
                self._spoke = True
                self._turn_reply += tail + " "
                if self._ui and tail:
                    self._ui.reply_append(tail)
            self._buf = ""
            self._active_until = time.monotonic() + self._active_window
            if self._transcript and self._turn_reply.strip():
                self._transcript.assistant(self._turn_reply)
            if self._ui:
                self._ui.done()

    def _next_filler(self) -> str:
        f = self._fillers[self._filler_i % len(self._fillers)]
        self._filler_i += 1
        return f

    def _remember_speech(self, text: str):
        """Guarda o que a assistente FALOU (pro filtro anti-eco)."""
        words = set(_norm_words(text))
        if words:
            self._recent_speech.append((time.monotonic(), words))

    def _is_echo(self, text: str) -> bool:
        """True se o 'ouvido' é a propria voz dela voltando pelo alto-falante.
        Compara com o que ela falou nos ultimos ~8s (sobreposicao de palavras)."""
        words = _norm_words(text)
        if len(words) < 2:
            return False
        now = time.monotonic()
        spoken = set()
        for t, ws in self._recent_speech:
            if now - t <= 8.0:
                spoken |= ws
        if not spoken:
            return False
        hit = sum(1 for w in set(words) if w in spoken)
        return hit / len(set(words)) >= 0.7

    async def _say(self, text: str) -> bool:
        clean = _clean_for_speech(text)
        if not clean:
            return False
        self._remember_speech(clean)
        await self.push_frame(TTSSpeakFrame(clean), FrameDirection.DOWNSTREAM)
        return True

    def _drain(self, buf: str):
        sents = []
        while True:
            m = _SENT_BOUNDARY.search(buf)
            if not m:
                break
            end = m.end()
            s = buf[:end].strip()
            buf = buf[end:]
            if s:
                sents.append(s)
        return buf, sents

    def _watch_decision(self, awaiting: bool, idle: float, in_tool: bool = False):
        """Puro/testável: dado (esperando 1a resposta?, segundos ocioso, tool rodando?)
        decide o que o watchdog faz -> ('none'|'hint'|'recover', mensagem)."""
        if awaiting:                                   # nenhuma resposta ainda neste turno
            if idle >= self._first_resp_timeout:       # resume pesado / daemon morto -> recupera
                return ("recover", "sem resposta (resume pesado ou daemon morto)")
            if idle >= 12:
                return ("hint", "demorando… (recomeço sozinho se travar)")
            return ("none", "")
        # já respondeu. Tool rodando (Bash/Task pode levar minutos SEM emitir nada) usa o
        # teto de tool; senão, stall normal — nunca matar um build legítimo no meio.
        limit = self._tool_timeout if in_tool else self._stall_timeout
        if idle >= limit:
            return ("recover", "travou no meio do turno")
        return ("none", "")

    async def _slow_watch(self, seq: int):
        """Watchdog do turno: avisa se demora e RECUPERA (mata + respawn FRESH) se travar,
        pra nunca ficar preso em 'pensando' pra sempre."""
        hinted = False
        try:
            while True:
                await asyncio.sleep(3)
                if seq != self._turn_seq or not self._turn_active:
                    return                              # turno acabou ou foi substituído
                idle = time.monotonic() - self._last_activity
                action, msg = self._watch_decision(self._awaiting, idle, self._in_tool)
                if action == "hint" and not hinted and self._ui:
                    self._ui.hint(msg)
                    hinted = True
                elif action == "recover":
                    await self._recover(msg, seq)
                    return
        except asyncio.CancelledError:
            pass

    async def _recover(self, reason: str, seq: int):
        """Sai do travamento: mata o daemon e recomeça FRESH (sem re-resume da sessão pesada,
        que é a causa do trava). Se o turno travado era read-only (modo chat), REFAZ sozinho
        o último pedido — sem fazer o usuário repetir. Em modo code pode ter mutação no meio,
        então não refaz cego: pede pra repetir."""
        if seq != self._turn_seq:
            return
        logger.warning(f"[brain] watchdog recuperando ({reason}) — respawn FRESH")
        # read-only e ainda não refez? -> dá pra refazer sozinho com segurança.
        safe_redo = self._mode == "chat" and bool(self._last_text) and not self._retried
        self._kill()
        self._proc = None
        self._resume = None                    # FRESH: NÃO re-resume a sessão pesada
        self._session_id = str(uuid.uuid4())
        self._turn_active = False
        self._awaiting = False
        self._discard = True                   # descarta qualquer cauda do turno morto
        self._buf = ""
        if self._ui:
            self._ui.set_session(self._session_id)
        if safe_redo:
            self._retried = True
            if self._ui:
                self._ui.status("refazendo…")
            await self._say("Perai, deixa eu refazer isso.")
            await self._send(self._last_text, is_retry=True)
            return
        if self._ui:
            self._ui.error("travei e recomecei do zero — pode repetir?")
            self._ui.status("ouvindo")
        await self._say(self._recover_phrase)  # feedback por voz (usuário pode não estar olhando)

    def _desired_mode(self, text: str) -> str:
        """Escolhe chat<->code. Com set_model o flip é BARATO (uma mensagem de controle,
        sem respawn/resume), mas as regras continuam conservadoras:
          1. Mutação explícita ('conserta', 'fix', 'deploy'...) -> 'code'.
          2. 'só analisa / não executa' ou pergunta de investigação -> read-only: FICA no
             modo atual (não precisa do modelo forte pra explicar).
          3. Fala neutra ('ok', 'agora testa') -> stickiness: fica no modo atual na janela."""
        now = time.monotonic()
        sticky = now < self._active_until
        if self._mutate_re.search(text):
            return "code"
        if self._no_exec_re.search(text) or self._analyze_re.search(text):
            return self._mode if sticky else "chat"   # read-only: sem flip
        if self._mode == "code" and sticky:
            return "code"
        return "chat"

    async def _switch_mode(self, mode: str):
        """Troca chat<->code (modelo). Daemon vivo: set_model via control_request — a
        MESMA sessão continua, sem kill + --resume (que pagava o prefill inteiro e era a
        causa raiz do 'travei e recomecei'). Validado no CLI 2.1.200. Fallback (CLI
        antigo): o caminho velho de respawn. Nota: o --effort é flag de spawn; num switch
        por control o effort do daemon fica o do boot (o ganho de nunca travar/perder a
        sessão vale mais que xhigh vs medium no modo código)."""
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "code":
            self._model, self._effort = self._code_model, self._code_effort
        else:
            self._model, self._effort = self._chat_model, self._chat_effort
        if self._ui:
            self._ui.hint("modo código…" if mode == "code" else "modo voz")
        logger.info(f"[brain] modo={mode} model={self._model} effort={self._effort}")
        if self._proc is None or self._proc.returncode is not None:
            return                              # próximo _ensure_proc já sobe com o novo
        target = self._model or self._default_model
        if target and await self._control("set_model", model=target):
            logger.info(f"[brain] set_model -> {target} (sem respawn)")
            return
        # fallback: CLI sem set_model (ou alvo desconhecido) — caminho antigo
        logger.info("[brain] set_model indisponível; respawn com --resume")
        self._resume = self._session_id     # mantem a conversa no novo modelo
        self._kill()
        self._proc = None
        await self._ensure_proc()

    async def inject_text(self, text: str):
        """Injeta um turno de TEXTO (colado/digitado) como se você tivesse falado."""
        text = (text or "").strip()
        if not text:
            return
        if self._turn_active:
            await self.interrupt_turn()
        await self._switch_mode(self._desired_mode(text))
        await self._send(text)

    async def _open_tab_bg(self):
        """Abre a aba do lado fora do event loop (osascript bloqueia ~ segundos)."""
        try:
            await asyncio.to_thread(self._ui.open_window)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[brain] open tab: {e}")
            if self._ui:
                self._ui.error(f"abrir aba: {e}", e)

    def _kill(self):
        if not (self._proc and self._proc.returncode is None):
            return
        try:
            if os.name == "nt":
                # Windows nao tem process groups/SIGKILL — derruba a arvore via taskkill
                # (o `claude` CLI roda em Node e pode ter filhos; /T pega todos).
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # grava a fala que passa pelo pipeline (ex.: greeting), pro filtro anti-eco
        if isinstance(frame, TTSSpeakFrame) and (frame.text or "").strip():
            self._remember_speech(frame.text)

        if isinstance(frame, UserStartedSpeakingFrame):
            # decide AQUI (no início da fala) se ela vale. Mutar DEPOIS não descarta a fala
            # que já começou — ela termina de transcrever e executar.
            self._beeped = False        # beep de utterance anterior não vale pra esta
            self._ignore_utterance = bool(self._ui and self._ui.muted)
            # Barge-in imediato SÓ quando a fala é provavelmente pra ela: open mic, ou
            # wake mode DENTRO da janela ativa. Com wake fora da janela, conversa de
            # fundo NÃO pode matar a resposta em andamento — se a transcrição passar o
            # gate, o interrupt_turn() corta lá (de verdade, geração + tools).
            if not self._ignore_utterance and \
               (not self._wake or time.monotonic() < self._active_until):
                self._discard = True
                self._buf = ""
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            # marca o fim da fala (latência STT) e dá o 'captei' JÁ — sem esperar o STT
            # (o beep pós-transcrição parecia travado). Só quando a fala é pra ela.
            self._t_vadstop = time.monotonic()
            if not self._ignore_utterance and not (self._ui and self._ui.muted) and \
               (not self._wake or time.monotonic() < self._active_until):
                from sounds import play
                play("heard")
                self._beeped = True
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame) and (frame.text or "").strip():
            text = frame.text.strip()
            if self._t_vadstop:
                self._last_stt_ms = (time.monotonic() - self._t_vadstop) * 1000
            if self._ignore_utterance or (self._ui and self._ui.muted):
                # fala que COMEÇOU mutada, ou mute apertado no meio da fala -> ignora
                # (o EchoGate já fecha o mic; isto pega o parcial que já estava no buffer).
                logger.debug(f"[brain] mutado, ignorando: {text!r}")
                return
            if _INTERRUPT_RE.match(text):  # "ei"/"para" = interrompe DE VERDADE
                await self.interrupt_turn()
                if self._ui:
                    self._ui.status("ouvindo")
                logger.debug(f"[brain] interrompido por: {text!r}")
                return
            if self._is_echo(text):       # a propria voz dela voltando pelo alto-falante
                logger.debug(f"[brain] eco ignorado: {text!r}")
                return
            if not self._should_answer(text):
                logger.debug(f"[brain] gated (sem '{'/'.join(self._wake)}'): {text!r}")
                return
            # disse a wake word -> tira ela do pedido (é só gatilho). Se só disse o nome
            # (ex.: "claude", sem comando), abre a janela ativa pro próximo turno não repetir.
            if self._wake and self._has_wake(text):
                text = self._strip_wake(text)
                if self._ui:
                    self._ui.status("captei")   # feedback imediato: "ouvi você"
                from sounds import play as _play; _play("wake")
                if not text:
                    self._active_until = time.monotonic() + self._active_window
                    logger.debug("[brain] wake word só, abrindo janela ativa")
                    # volta pra "ouvindo" após 1.5s (só confirmou que ouviu)
                    async def _reset_captei():
                        await asyncio.sleep(1.5)
                        if self._ui and self._ui._status == "captei":
                            self._ui.status("ouvindo")
                    self.create_task(_reset_captei())
                    return
            # comando de voz: abrir a aba "Claude Code ao vivo" (sem travar a call)
            if self._ui and self._show_re.search(text):
                self._ui.status("abrindo a aba…")
                self.create_task(self._open_tab_bg())
                await self._say("Abrindo a aba aqui do lado.")
                return
            # anti-burn: teto de invocacoes/hora ANTES de gastar credito de agente.
            if self._rate_limited():
                logger.warning(f"[brain] rate limit ({self._max_per_hour}/h) atingido, ignorando: {text!r}")
                if self._ui:
                    self._ui.status("limite/ora raggiunto")
                await self._say("Ho raggiunto il limite di comandi per ora, aspetta un attimo.")
                return
            # fala nova no meio de um turno: interrompe o antigo DE VERDADE antes de
            # mandar o novo (senão o daemon seguia gerando/executando o descartado).
            if self._turn_active:
                await self.interrupt_turn()
            # programar? troca pro modelo forte; senão fica no chat (rápido)
            await self._switch_mode(self._desired_mode(text))
            await self._send(text)
            return

        await self.push_frame(frame, direction)

    async def cleanup(self):
        await super().cleanup()
        if self._reader:
            self._reader.cancel()
        self._kill()
