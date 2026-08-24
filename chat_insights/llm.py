"""
General-purpose local model client (Ollama).

Deliberately separate from content_seo_agent/small_model_client.py. That one is
bound to the fine-tuned product-description model and its two fixed system
prompts; it is trained to emit product JSON and would be useless -- actively
misleading -- on a chat transcript. This module takes an arbitrary prompt and a
configurable model, so chat analysis can use a general instruct model while the
content pipeline keeps its specialist.

Shares small_model_client's fence-tolerant JSON extraction: small models
routinely wrap JSON in markdown or add a sentence of preamble.
"""
import json
import logging

import requests

from config import settings

logger = logging.getLogger("chat_insights.llm")

REQUEST_TIMEOUT_SECONDS = 180  # a 3B model on CPU is slow; don't cut it off early


def _chat_url() -> str:
    return settings.OLLAMA_URL


def _tags_url() -> str:
    """Ollama's model-list endpoint, derived from the configured chat URL so
    both follow the same host/port without a second config entry."""
    return _chat_url().rsplit("/api/", 1)[0] + "/api/tags"


def installed_models() -> list[str]:
    try:
        resp = requests.get(_tags_url(), timeout=5)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except requests.RequestException:
        return []


def is_available(model: str | None = None) -> tuple[bool, str]:
    """Returns (ok, human-readable reason). Checks the specific model is
    actually pulled rather than just that Ollama is up -- the usual failure
    here is a running Ollama that has never been given this model."""
    model = model or settings.CHAT_MODEL
    models = installed_models()
    if not models:
        return False, f"Ollama is not reachable at {_chat_url()}."
    # Ollama reports "llama3.2:3b"; a bare "llama3.2" should still match.
    if model in models or any(m.split(":")[0] == model.split(":")[0] for m in models):
        return True, f"{model} is available."
    return False, (f"Model '{model}' is not installed. Run:  ollama pull {model}\n"
                    f"Installed: {', '.join(models) or '(none)'}")


def _extract_json(raw_text: str) -> tuple[dict | None, bool]:
    """Direct parse first, then the first {...} block. Same tolerance as
    content_seo_agent/small_model_client.py, for the same reason."""
    raw_text = (raw_text or "").strip()
    try:
        parsed = json.loads(raw_text)
        return (parsed, True) if isinstance(parsed, dict) else (None, False)
    except json.JSONDecodeError:
        pass
    start, end = raw_text.find("{"), raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(raw_text[start:end + 1])
            return (parsed, True) if isinstance(parsed, dict) else (None, False)
        except json.JSONDecodeError:
            return None, False
    return None, False


def complete(system_prompt: str, user_prompt: str, *, model: str | None = None,
             temperature: float | None = None, max_tokens: int | None = None) -> dict:
    """Runs an arbitrary prompt. Returns the same result shape the rest of this
    codebase uses:
        {"parsed": dict|None, "parse_success": bool, "raw_text": str, "error": str|None}
    so callers can branch on failure without re-deriving that logic."""
    payload = {
        "model": model or settings.CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",  # Ollama constrains output to valid JSON where supported
        "options": {
            "temperature": settings.CHAT_TEMPERATURE if temperature is None else temperature,
            "num_predict": settings.CHAT_MAX_ANALYSIS_TOKENS if max_tokens is None else max_tokens,
        },
    }
    try:
        resp = requests.post(_chat_url(), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        raw_text = resp.json().get("message", {}).get("content", "")
    except requests.RequestException as e:
        logger.error("Chat model call failed: %s", e)
        return {"parsed": None, "parse_success": False, "raw_text": "", "error": str(e)}

    parsed, ok = _extract_json(raw_text)
    return {"parsed": parsed, "parse_success": ok, "raw_text": raw_text, "error": None}
