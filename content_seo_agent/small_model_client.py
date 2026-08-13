"""
Client for the fine-tuned small model, served locally via Ollama.

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
REQUEST_TIMEOUT_SECONDS = 60


def _call_ollama(system_prompt: str, user_prompt: str) -> dict:
    """Low-level call to Ollama's chat endpoint. Returns the raw response dict."""
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.3},
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


def classify(title: str, description: str = "") -> dict:
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
        response = _call_ollama(settings.SYSTEM_CLASSIFY, user_prompt)
        raw_text = response.get("message", {}).get("content", "")
    except requests.RequestException as e:
        logger.error("Ollama classify call failed: %s", e)
        return {"parsed": None, "parse_success": False, "raw_text": "", "error": str(e)}

    parsed, success = _extract_json(raw_text)
    return {"parsed": parsed, "parse_success": success, "raw_text": raw_text, "error": None}


def draft(title: str) -> dict:
    """
    Runs the drafting task. Same return shape as classify().
    """
    user_prompt = f"Product title: {title}"

    try:
        response = _call_ollama(settings.SYSTEM_DRAFT, user_prompt)
        raw_text = response.get("message", {}).get("content", "")
    except requests.RequestException as e:
        logger.error("Ollama draft call failed: %s", e)
        return {"parsed": None, "parse_success": False, "raw_text": "", "error": str(e)}

    parsed, success = _extract_json(raw_text)
    return {"parsed": parsed, "parse_success": success, "raw_text": raw_text, "error": None}


def is_available() -> bool:
    """Quick health check -- used at orchestrator startup and by the dashboard status tab."""
    try:
        resp = requests.get("http://127.0.0.1:11434/api/tags", timeout=5)
        return resp.status_code == 200
    except requests.RequestException:
        return False
