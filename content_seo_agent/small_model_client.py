"""
Client for local models served via Ollama.

Serves both rungs of the local half of the escalation chain: the fine-tuned
model (settings.SMALL_MODEL_NAME) by default, and any other Ollama tag passed
as `model` -- which is how the general fallback model is called with the same
prompts and the same parsing.

Handles both task types (classify / draft) using the same system
prompts the model was trained on. Returns a structured result that
always tells the caller whether parsing succeeded, so the orchestrator
can make an escalation decision without re-deriving that logic.
"""
import json
import logging

import requests

from config import settings

logger = logging.getLogger("small_model_client")

OLLAMA_URL = settings.OLLAMA_URL
MODEL_NAME = settings.SMALL_MODEL_NAME
TAGS_URL = "http://127.0.0.1:11434/api/tags"
REQUEST_TIMEOUT_SECONDS = 60


DEFAULT_TEMPERATURE = 0.3


def _call_ollama(system_prompt: str, user_prompt: str, temperature: float | None = None,
                  model: str | None = None) -> dict:
    """Low-level call to Ollama's chat endpoint. Returns the raw response dict.

    model: overrides the fine-tuned model -- used for the fallback rung of the
    escalation chain."""
    payload = {
        "model": model or MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": DEFAULT_TEMPERATURE if temperature is None else temperature},
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _extract_json(raw_text: str) -> tuple[dict | None, bool]:
    """
    Small models often wrap JSON in markdown fences or add stray text.
    Try a direct parse first, then fall back to extracting the first
    {...} block. Returns (parsed_dict_or_None, parse_success).
    """
    raw_text = raw_text.strip()
    parsed = None
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = raw_text[start : end + 1]
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                return None, False
        else:
            return None, False

    return _normalize_legacy_fields(parsed), True


# If you fine-tuned a model before adopting this generic config system
# (e.g. it still outputs "is_stabilised" instead of "is_regulated"),
# add the mapping here rather than retraining. Costs nothing for
# models already using current field names.
_LEGACY_FIELD_ALIASES = {
    "is_stabilised": "is_regulated",
}


def _normalize_legacy_fields(parsed: dict | None) -> dict | None:
    if not isinstance(parsed, dict):
        return parsed
    for old_key, new_key in _LEGACY_FIELD_ALIASES.items():
        if old_key in parsed and new_key not in parsed:
            parsed[new_key] = parsed.pop(old_key)
    return parsed


def classify(title: str, description: str = "", model: str | None = None) -> dict:
    """
    Runs the classification task. Returns:
    {
        "parsed": {...} or None,
        "parse_success": bool,
        "raw_text": str,
        "error": str or None,
    }
    """
    desc_snippet = description[:400] if description else "(no description)"
    user_prompt = f"Product title: {title}\nCurrent description: {desc_snippet}"

    try:
        response = _call_ollama(settings.SYSTEM_CLASSIFY, user_prompt, model=model)
        raw_text = response.get("message", {}).get("content", "")
    except requests.RequestException as e:
        logger.error("Ollama classify call failed (%s): %s", model or MODEL_NAME, e)
        return {"parsed": None, "parse_success": False, "raw_text": "", "error": str(e)}

    parsed, success = _extract_json(raw_text)
    return {"parsed": parsed, "parse_success": success, "raw_text": raw_text, "error": None}


def draft(title: str, temperature: float | None = None, model: str | None = None,
           extra_context: str = "") -> dict:
    """
    Runs the drafting task. Same return shape as classify().

    temperature: overrides the default. Regeneration passes a higher value --
    at the default 0.3 a re-run on the same title tends to reproduce almost
    exactly the draft a reviewer just rejected, which defeats the point of
    retrying.

    extra_context: grounding info worded exactly as claude_client.draft() words
    it, so a retry reaching the fallback model instead of Claude carries the
    same reviewer feedback.
    """
    user_prompt = f"Product title: {title}"
    if extra_context:
        user_prompt += "\nKnown attributes: " + extra_context

    try:
        response = _call_ollama(settings.SYSTEM_DRAFT, user_prompt, temperature=temperature,
                                 model=model)
        raw_text = response.get("message", {}).get("content", "")
    except requests.RequestException as e:
        logger.error("Ollama draft call failed (%s): %s", model or MODEL_NAME, e)
        return {"parsed": None, "parse_success": False, "raw_text": "", "error": str(e)}

    parsed, success = _extract_json(raw_text)
    return {"parsed": parsed, "parse_success": success, "raw_text": raw_text, "error": None}


def installed_models() -> list[str]:
    """Model tags Ollama currently has. Empty when Ollama is unreachable."""
    try:
        resp = requests.get(TAGS_URL, timeout=5)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except (requests.RequestException, ValueError):
        return []


def has_model(name: str) -> bool:
    """True when `name` is pullable right now. Ollama reports "llama3.2:3b"
    while config may say "llama3.2", so match on the tag-less stem too."""
    if not name:
        return False
    installed = installed_models()
    return any(tag == name or tag.split(":")[0] == name.split(":")[0] for tag in installed)


def is_available() -> bool:
    """Quick health check -- used at orchestrator startup and by the dashboard status tab."""
    try:
        resp = requests.get(TAGS_URL, timeout=5)
        return resp.status_code == 200
    except requests.RequestException:
        return False
