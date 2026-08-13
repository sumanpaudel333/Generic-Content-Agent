"""
Escalation client -- calls the Anthropic API when the small local model
is low-confidence, fails to parse, or trips the safety filter.

Uses the same JSON contract as small_model_client.py so Stage 4
(assembly) never needs to know or care which model produced a draft.
"""
import json
import logging
import os
import requests

from config import settings

logger = logging.getLogger("claude_client")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL_NAME = settings.CLAUDE_MODEL
REQUEST_TIMEOUT_SECONDS = 60


def _call_claude(system_prompt: str, user_prompt: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment (.env)")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "max_tokens": 1000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _extract_json(raw_text: str) -> tuple[dict | None, bool]:
    raw_text = raw_text.strip()
    try:
        return json.loads(raw_text), True
    except json.JSONDecodeError:
        pass
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = raw_text[start : end + 1]
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            pass
    return None, False


def classify(title: str, description: str = "") -> dict:
    desc_snippet = description[:1500] if description else "(no description)"
    user_prompt = f"Product title: {title}\nCurrent description: {desc_snippet}"

    try:
        response = _call_claude(settings.SYSTEM_CLASSIFY, user_prompt)
        raw_text = "".join(
            block.get("text", "") for block in response.get("content", []) if block.get("type") == "text"
        )
    except (requests.RequestException, RuntimeError) as e:
        logger.error("Claude classify call failed: %s", e)
        return {"parsed": None, "parse_success": False, "raw_text": "", "error": str(e)}

    parsed, success = _extract_json(raw_text)
    return {"parsed": parsed, "parse_success": success, "raw_text": raw_text, "error": None}


def draft(title: str, extra_context: str = "") -> dict:
    """
    extra_context: optional grounding info (e.g. real attributes pulled
    from Odoo) to reduce fabrication risk on escalated/harder products.
    """
    user_prompt = f"Product title: {title}"
    if extra_context:
        user_prompt += f"\nKnown attributes: {extra_context}"

    try:
        response = _call_claude(settings.SYSTEM_DRAFT, user_prompt)
        raw_text = "".join(
            block.get("text", "") for block in response.get("content", []) if block.get("type") == "text"
        )
    except (requests.RequestException, RuntimeError) as e:
        logger.error("Claude draft call failed: %s", e)
        return {"parsed": None, "parse_success": False, "raw_text": "", "error": str(e)}

    parsed, success = _extract_json(raw_text)
    return {"parsed": parsed, "parse_success": success, "raw_text": raw_text, "error": None}
