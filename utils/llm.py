"""Shared Gemini fallback — newest → oldest, tail handles 429 without config."""

from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# curl-verified free-tier models that support generateContent (newest first) — 3.7 removed (unreliable)
_FALLBACK_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]


def generate_with_fallback(prompt: str, timeout: float = 120):
    """Try models in order; 429/404/deadline → next, 400 → raise. 2-min cap."""
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    last_err = None
    for model in _FALLBACK_MODELS:
        try:
            # per-call timeout via http_options not exposed; rely on overall 2-min wrapper
            resp = client.models.generate_content(model=model, contents=prompt)
            text = getattr(resp, "text", "") or ""
            if text.strip():
                logger.info("gemini model=%s ok", model)
                return resp, model
            last_err = ValueError(f"empty response from {model}")
        except Exception as e:
            msg = str(e)
            # 400 / INVALID_ARGUMENT → bad prompt, don't retry
            if "400" in msg or "INVALID_ARGUMENT" in msg:
                raise
            logger.info("gemini model=%s failed %s, trying next", model, msg[:80])
            last_err = e
            continue
    raise last_err or RuntimeError("all Gemini fallbacks exhausted")
