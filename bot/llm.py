"""Optional Claude calls. Everything here returns None when no API key is configured."""

import json
import logging

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"


def _client(api_key: str):
    if not api_key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=api_key, timeout=120.0)


def ask_json(api_key: str, system: str, prompt: str, schema: dict, effort: str = "low") -> dict | None:
    client = _client(api_key)
    if client is None:
        return None
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # network/API failure must never stop the bot
        log.warning("Claude request failed: %s", type(e).__name__)
        return None
    if resp.stop_reason != "end_turn":
        log.warning("Claude stopped with %s", resp.stop_reason)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def ask_text(api_key: str, system: str, prompt: str, effort: str = "medium") -> str | None:
    client = _client(api_key)
    if client is None:
        return None
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning("Claude request failed: %s", type(e).__name__)
        return None
    if resp.stop_reason not in ("end_turn", "max_tokens"):
        log.warning("Claude stopped with %s", resp.stop_reason)
        return None
    return "".join(b.text for b in resp.content if b.type == "text") or None
