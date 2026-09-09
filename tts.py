"""TTS via Microsoft edge-tts (gratis, online, vozes neurais em varios idiomas).

Pipecat nao traz edge-tts embutido, entao implementamos um TTSService:
edge-tts gera MP3 -> ffmpeg decodifica pra PCM s16le mono -> frames de audio raw.
"""
import asyncio
import hashlib
import importlib
import os
import uuid
from pathlib import Path
from typing import AsyncGenerator

import edge_tts
from loguru import logger

# Cache de PCM pra frases CURTAS e repetidas (fillers 'Peraí', greeting): TTFB ~0ms em
# vez de um RTT do edge-tts — exatamente no momento cuja função é mascarar latência.
_CACHE_DIR = Path.home() / ".cache" / "claude-call" / "tts"
_CACHE_MAX_CHARS = 80

from pipecat.frames.frames import (
    ErrorFrame, Frame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame,
)
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService


class EdgeTTS(TTSService):
    def __init__(self, *, voice: str, rate: str = "+0%", pitch: str = "+0Hz",
                 sample_rate: int = 24000, **kwargs):
        # Inicializa os campos de settings (senao Pipecat loga ERROR de NOT_GIVEN).
        super().__init__(sample_rate=sample_rate,
                         settings=TTSSettings(model=None, voice=voice, language=None),
                         **kwargs)
        self._voice, self._rate, self._pitch = voice, rate, pitch

    def can_generate_metrics(self) -> bool:
        return False

    def _cache_path(self, text: str) -> Path:
        key = f"{self._voice}|{self._rate}|{self._pitch}|{self.sample_rate}|{text}"
        return _CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".pcm")

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Streaming: feed edge-tts mp3 into ffmpeg as it arrives, emit PCM as it
        decodes — first audio starts in ~hundreds of ms instead of after the whole
        sentence is generated. `-f mp3` skips format probing so ffmpeg starts at once.
        Frases curtas repetidas (fillers/greeting) saem do cache em disco: TTFB ~0ms."""
        logger.debug(f"[edge-tts] {text!r}")
        frame_bytes = int(self.sample_rate * 0.02) * 2  # ~20ms
        cacheable = len(text) <= _CACHE_MAX_CHARS
        if cacheable:
            cp = self._cache_path(text)
            if cp.exists():
                try:
                    pcm = cp.read_bytes()
                except OSError:
                    pcm = b""
                if pcm:
                    yield TTSStartedFrame()
                    for i in range(0, len(pcm), frame_bytes):
                        yield TTSAudioRawFrame(audio=pcm[i:i + frame_bytes],
                                               sample_rate=self.sample_rate, num_channels=1)
                    yield TTSStoppedFrame()
                    return
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "mp3", "-i", "pipe:0",
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ac", "1", "-ar", str(self.sample_rate), "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            yield ErrorFrame("ffmpeg not found")
            return

        async def feed():
            try:
                comm = edge_tts.Communicate(text, self._voice, rate=self._rate, pitch=self._pitch)
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        proc.stdin.write(chunk["data"])
                        await proc.stdin.drain()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[edge-tts] stream: {e}")
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

        feeder = asyncio.create_task(feed())
        await self.start_ttfb_metrics()
        yield TTSStartedFrame()
        got = False
        pcm_out = bytearray() if cacheable else None
        try:
            while True:
                data = await proc.stdout.read(frame_bytes)
                if not data:
                    break
                got = True
                if pcm_out is not None:
                    pcm_out.extend(data)
                yield TTSAudioRawFrame(audio=data, sample_rate=self.sample_rate, num_channels=1)
        finally:
            if not feeder.done():
                feeder.cancel()
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        if not got:
            yield ErrorFrame("edge-tts produced no audio")
        elif pcm_out:
            try:   # grava atômico (tmp único + rename): prewarm e pipeline podem gerar a
                   # MESMA frase ao mesmo tempo no mesmo processo — pid só não bastaria.
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = cp.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
                tmp.write_bytes(bytes(pcm_out))
                tmp.replace(cp)
            except OSError:
                pass
        yield TTSStoppedFrame()


async def prewarm_edge_cache(texts, *, voice: str, rate: str, sample_rate: int):
    """Sintetiza (em background, no startup) as frases fixas — fillers e greeting — pra
    já estarem no cache quando a call precisar delas. Falha silenciosa (sem rede etc.)."""
    svc = EdgeTTS(voice=voice, rate=rate, sample_rate=sample_rate)
    # fora da pipeline o sample_rate só é aplicado no StartFrame — seta direto
    # (mesmo truque do doctor), senão frame_bytes=0 e sai "no audio" silencioso.
    svc._sample_rate = sample_rate
    for t in texts:
        if not t or len(t) > _CACHE_MAX_CHARS or svc._cache_path(t).exists():
            continue
        try:
            async for _ in svc.run_tts(t, "prewarm"):
                pass
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[tts] prewarm {t!r}: {e}")


# ---------------------------------------------------------------- providers
# Premium TTS via Pipecat's built-in services. Bring your own API key (pay-as-you-go).
# edge = free (default). Others need CALL_TTS_API_KEY (or the provider's standard env var).

# Voices that are opaque ids (must be set by the user) vs named defaults we can pick.
_PREMIUM_DEFAULTS = {
    # provider: (default_voice_or_None, default_model_or_None, env_key_name)
    "elevenlabs": ("21m00Tcm4TlvDq8ikWAM", "eleven_turbo_v2_5", "ELEVENLABS_API_KEY"),
    "cartesia":   (None,                    "sonic-2",           "CARTESIA_API_KEY"),
    "openai":     ("alloy",                 "gpt-4o-mini-tts",   "OPENAI_API_KEY"),
    "rime":       ("cove",                  "mistv2",            "RIME_API_KEY"),
    "deepgram":   ("aura-2-thalia-en",      None,                "DEEPGRAM_API_KEY"),
}

_SVC = {
    "elevenlabs": "pipecat.services.elevenlabs.tts:ElevenLabsTTSService",
    "cartesia":   "pipecat.services.cartesia.tts:CartesiaTTSService",
    "openai":     "pipecat.services.openai.tts:OpenAITTSService",
    "rime":       "pipecat.services.rime.tts:RimeTTSService",
    "deepgram":   "pipecat.services.deepgram.tts:DeepgramTTSService",
}

PROVIDERS = ["edge"] + list(_PREMIUM_DEFAULTS)


def make_tts(*, provider: str, voice: str | None, rate: str, sample_rate: int,
             api_key: str | None = None, model: str | None = None):
    """Returns a Pipecat TTS service for the chosen provider.
    edge = free/local-ish; the rest are paid APIs (bring your own key)."""
    p = (provider or "edge").lower()
    if p == "edge":
        return EdgeTTS(voice=voice or "en-US-AndrewNeural", rate=rate, sample_rate=sample_rate)

    if p not in _PREMIUM_DEFAULTS:
        raise ValueError(f"unknown CALL_TTS '{provider}'. options: {', '.join(PROVIDERS)}")

    def_voice, def_model, env_key = _PREMIUM_DEFAULTS[p]
    key = api_key or os.getenv(env_key) or os.getenv("CALL_TTS_API_KEY")
    if not key:
        raise RuntimeError(f"{p} needs an API key — set CALL_TTS_API_KEY (or {env_key}).")
    v = voice or def_voice
    if v is None:
        raise RuntimeError(f"{p} needs a voice id — set CALL_VOICE to a {p} voice.")
    m = model or def_model

    # All Pipecat TTS services share the shape: Service(api_key, sample_rate,
    # settings=Service.Settings(voice=..., model=...)). Uniform = no deprecations.
    mod, cls = _SVC[p].split(":")
    Svc = getattr(importlib.import_module(mod), cls)
    sargs = {"voice": v}
    if m:
        sargs["model"] = m
    return Svc(api_key=key, sample_rate=sample_rate, settings=Svc.Settings(**sargs))
