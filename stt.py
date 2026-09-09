"""STT local via whisper.cpp. Usa whisper-server (modelo residente, ~0.6s/fala) e
cai pro whisper-cli (recarrega o modelo por fala, ~2s) se o server nao subir.
"""
import asyncio
import base64
import io
import os
import subprocess
import tempfile
import warnings
import wave
from pathlib import Path
from typing import AsyncGenerator

with warnings.catch_warnings():  # audioop is fine on 3.12 (pinned); silence its 3.13 notice
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

import httpx
from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601


def _stt_settings() -> STTSettings:
    # Inicializa os campos (model/language) com None — somos nos que mandamos
    # language no request HTTP. Sem isso o Pipecat loga ERROR de campos NOT_GIVEN.
    return STTSettings(model=None, language=None)

WHISPER_RATE = 16000
LOG_FILE = Path(__file__).resolve().parent / "logs" / "whisper-server.log"


def _to_16k(audio: bytes, rate: int) -> bytes:
    if rate and rate != WHISPER_RATE:
        audio, _ = audioop.ratecv(audio, 2, 1, rate, WHISPER_RATE, None)
    return audio


def _wav(audio: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(WHISPER_RATE)
        w.writeframes(audio)
    return buf.getvalue()


async def ensure_server(*, model: str, host="127.0.0.1", port=8099, language="en",
                        threads=6, wait_secs=60.0) -> str:
    base = f"http://{host}:{port}"

    async def up() -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.0) as c:
                await c.get(base + "/")
            return True
        except Exception:
            return False

    if await up():
        logger.info(f"[whisper-server] already up at {base}")
        return base
    if not Path(model).exists():
        raise FileNotFoundError(f"whisper model not found: {model} (run scripts/download-model.sh)")

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_FILE, "ab")
    logger.info(f"[whisper-server] starting ({Path(model).name})...")
    subprocess.Popen(
        ["whisper-server", "-m", model, "--host", host, "--port", str(port),
         "-l", language, "-nt", "-t", str(threads)],
        stdout=logf, stderr=logf, start_new_session=True,
    )
    deadline = asyncio.get_event_loop().time() + wait_secs
    while asyncio.get_event_loop().time() < deadline:
        if await up():
            logger.info(f"[whisper-server] ready at {base}")
            return base
        await asyncio.sleep(0.5)
    raise RuntimeError(f"whisper-server did not start in {wait_secs}s (see {LOG_FILE})")


class WhisperServerSTT(SegmentedSTTService):
    def __init__(self, *, base_url: str, language="en", sample_rate=WHISPER_RATE, **kwargs):
        super().__init__(sample_rate=sample_rate, settings=_stt_settings(), **kwargs)
        self._url = base_url.rstrip("/") + "/inference"
        self._language = language
        self._client: httpx.AsyncClient | None = None

    def can_generate_metrics(self) -> bool:
        return False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        await self.start_ttfb_metrics()
        wav = _wav(_to_16k(audio, self.sample_rate or WHISPER_RATE))
        try:
            # NOTA: testamos response_format=verbose_json + filtro por no_speech_prob
            # (o clássico anti-alucinação) e ficou PIOR neste server: no silêncio, o
            # caminho json+no_timestamps retorna "" e o verbose_json alucina com
            # no_speech_prob ~0 (alem de JSON invalido intermitente). Mantido o formato
            # que na prática suprime melhor.
            resp = await self._client.post(
                self._url,
                files={"file": ("a.wav", wav, "audio/wav")},
                data={"language": self._language, "response_format": "json",
                      "no_timestamps": "true", "temperature": "0"},
            )
            text = " ".join(resp.json().get("text", "").split()).strip()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[whisper-server] inference failed: {e}")
            return
        if text:
            logger.debug(f"[whisper-server] -> {text!r}")
            yield TranscriptionFrame(text, "user", time_now_iso8601())


class WhisperCliSTT(SegmentedSTTService):
    def __init__(self, *, model: str, language="en", binary="whisper-cli",
                 sample_rate=WHISPER_RATE, threads=6, **kwargs):
        super().__init__(sample_rate=sample_rate, settings=_stt_settings(), **kwargs)
        self._model, self._language, self._binary, self._threads = model, language, binary, threads

    def can_generate_metrics(self) -> bool:
        return False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        audio = _to_16k(audio, self.sample_rate or WHISPER_RATE)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(WHISPER_RATE); w.writeframes(audio)
            proc = await asyncio.create_subprocess_exec(
                self._binary, "-m", self._model, "-f", tmp.name,
                "-l", self._language, "-nt", "-np", "-t", str(self._threads),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        if proc.returncode != 0:
            logger.error(f"[whisper-cli] error: {err.decode(errors='ignore')[:200]}")
            return
        text = " ".join(out.decode(errors="ignore").split()).strip()
        if text:
            yield TranscriptionFrame(text, "user", time_now_iso8601())


# ---------------------------------------------------------------- API providers
# STT hospedado (melhor qualidade que o whisper.cpp local, principalmente em PT).
# Cada provider precisa de uma key (CALL_STT_API_KEY ou a env padrao do provider).
#   provider: (env_key, default_model, url, kind)
# kind="elevenlabs" -> Scribe (xi-api-key, model_id, language_code)
# kind="openai"     -> /audio/transcriptions compativel (Bearer, model, language) — Groq e OpenAI
_STT_PROVIDERS = {
    "elevenlabs": ("ELEVENLABS_API_KEY", "scribe_v1",
                   "https://api.elevenlabs.io/v1/speech-to-text", "elevenlabs"),
    "groq":       ("GROQ_API_KEY", "whisper-large-v3-turbo",
                   "https://api.groq.com/openai/v1/audio/transcriptions", "openai"),
    "openai":     ("OPENAI_API_KEY", "gpt-4o-mini-transcribe",
                   "https://api.openai.com/v1/audio/transcriptions", "openai"),
    # Google Cloud Speech-to-Text v2 (Chirp 2) — SOTA p/ PT. Auth via ADC/gcloud (sem key).
    "google":     ("", "chirp_2", "", "google"),
}
STT_PROVIDERS = ["local"] + list(_STT_PROVIDERS)

# pt -> pt-BR etc. pro Google (languageCodes)
_G_LANG = {"pt": "pt-BR", "en": "en-US", "es": "es-ES", "fr": "fr-FR", "de": "de-DE",
           "it": "it-IT", "ja": "ja-JP"}


class ApiSTT(SegmentedSTTService):
    """STT por API (ElevenLabs Scribe / Groq / OpenAI / Google Chirp). Manda o audio da
    fala (16k mono wav) e devolve o texto. Sem servidor local, sem modelo no disco."""

    def __init__(self, *, provider: str, api_key: str, model: str, language="en",
                 sample_rate=WHISPER_RATE, **kwargs):
        super().__init__(sample_rate=sample_rate, settings=_stt_settings(), **kwargs)
        env_key, default_model, url, kind = _STT_PROVIDERS[provider]
        self._provider = provider
        self._url = url
        self._kind = kind
        self._key = api_key
        self._model = model or default_model
        self._language = language
        self._client: httpx.AsyncClient | None = None
        # google: credenciais ADC + endpoint regional (Chirp precisa de regiao, nao 'global')
        self._g_creds = None
        if kind == "google":
            self._g_lang = _G_LANG.get(language[:2], language)
            self._g_loc = os.getenv("CALL_GOOGLE_LOCATION", "us-central1")
            import google.auth
            self._g_creds, default_proj = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._g_proj = os.getenv("CALL_GOOGLE_PROJECT") or default_proj
            self._url = (f"https://{self._g_loc}-speech.googleapis.com/v2/projects/"
                         f"{self._g_proj}/locations/{self._g_loc}/recognizers/_:recognize")

    def can_generate_metrics(self) -> bool:
        return False

    def _g_token(self) -> str:
        from google.auth.transport.requests import Request
        if not self._g_creds.valid:
            self._g_creds.refresh(Request())
        return self._g_creds.token

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        await self.start_ttfb_metrics()
        wav = _wav(_to_16k(audio, self.sample_rate or WHISPER_RATE))
        try:
            if self._kind == "google":
                token = await asyncio.to_thread(self._g_token)
                body = {
                    "config": {"autoDecodingConfig": {}, "model": self._model,
                               "languageCodes": [self._g_lang]},
                    "content": base64.b64encode(wav).decode(),
                }
                resp = await self._client.post(
                    self._url, headers={"Authorization": f"Bearer {token}"}, json=body)
                resp.raise_for_status()
                results = resp.json().get("results", []) or []
                text = " ".join(
                    (r.get("alternatives") or [{}])[0].get("transcript", "") for r in results)
                text = " ".join(text.split()).strip()
            elif self._kind == "elevenlabs":
                resp = await self._client.post(
                    self._url,
                    headers={"xi-api-key": self._key},
                    data={"model_id": self._model, "language_code": self._language},
                    files={"file": ("a.wav", wav, "audio/wav")},
                )
                resp.raise_for_status()
                text = " ".join((resp.json().get("text") or "").split()).strip()
            else:  # openai-compativel (Groq, OpenAI)
                resp = await self._client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._key}"},
                    data={"model": self._model, "language": self._language,
                          "response_format": "json", "temperature": "0"},
                    files={"file": ("a.wav", wav, "audio/wav")},
                )
                resp.raise_for_status()
                text = " ".join((resp.json().get("text") or "").split()).strip()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[stt:{self._provider}] inference failed: {e}")
            return
        if text:
            logger.debug(f"[stt:{self._provider}] -> {text!r}")
            yield TranscriptionFrame(text, "user", time_now_iso8601())


async def make_stt(*, provider: str, api_key: str | None, api_model: str | None,
                   model: str, language: str, port: int, use_server: bool):
    """Cria o STT. provider='local' -> whisper.cpp (server, senao cli). Caso contrario,
    usa o provider de API (ElevenLabs/Groq/OpenAI), caindo pro local se faltar key."""
    provider = (provider or "local").lower()
    if provider != "local":
        if provider not in _STT_PROVIDERS:
            logger.warning(f"CALL_STT '{provider}' desconhecido; opcoes: {', '.join(STT_PROVIDERS)}. Usando local.")
        elif provider == "google":
            try:
                stt = ApiSTT(provider="google", api_key="", model=api_model or "", language=language)
                logger.info("STT: google chirp (API)")
                return stt
            except Exception as e:  # noqa: BLE001
                logger.warning(f"STT google indisponivel ({e}); caindo pro whisper local.")
        else:
            env_key = _STT_PROVIDERS[provider][0]
            key = api_key or os.getenv(env_key)
            if not key:
                logger.warning(f"STT '{provider}' sem key (CALL_STT_API_KEY ou {env_key}); caindo pro whisper local.")
            else:
                logger.info(f"STT: {provider} (API)")
                return ApiSTT(provider=provider, api_key=key, model=api_model or "", language=language)

    if use_server:
        try:
            base = await ensure_server(model=model, port=port, language=language)
            return WhisperServerSTT(base_url=base, language=language)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"whisper-server unavailable ({e}); using whisper-cli")
    return WhisperCliSTT(model=model, language=language)
