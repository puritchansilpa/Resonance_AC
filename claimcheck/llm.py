"""Shared plumbing for every Claude call: one request shape, one cache, one error type.

A request is a plain dict {model, effort, system, messages, schema}. Its SHA-256
(over the sorted JSON) is the cache key, so the same request always returns the
same stored response, and a cached response can be replayed without an API key.
Responses must match `schema` (structured output).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config

OPUS = "claude-opus-5-5"      # drafting rules and hypotheses: accuracy matters, few calls
HAIKU = "claude-haiku-5-5"    # one short message per claim: the brief recommends Haiku for per-claim calls
LLM_CACHE_DIR = config.REPO_ROOT / "llm_cache"

# Models that accept the server-side refusal fallback (Haiku 5.5 has none).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"}


class LLMError(RuntimeError):
    pass


def request_key(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def make_request(model: str, effort: str, system: str, messages: list[dict], schema: dict) -> dict:
    return {"model": model, "effort": effort, "system": system, "messages": messages, "schema": schema}


def call_claude(request: dict, client=None) -> dict:
    import anthropic

    client = client or anthropic.Anthropic()
    kwargs = dict(
        model=request["model"],
        max_tokens=16000,
        system=[{"type": "text", "text": request["system"], "cache_control": {"type": "ephemeral"}}],
        messages=request["messages"],
        output_config={"effort": request["effort"],
                       "format": {"type": "json_schema", "schema": request["schema"]}},
    )
    if request["model"] in _FALLBACK_MODELS:
        # On a safety decline, the API re-runs the request on a fallback model.
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise LLMError("The API key was rejected; check ANTHROPIC_API_KEY.") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("Rate limited by the API; try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Could not reach the API (network).") from exc

    if response.stop_reason == "refusal":
        raise LLMError("The model declined the request.")
    if response.stop_reason == "max_tokens":
        raise LLMError("The response was cut off at max_tokens.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise LLMError("No text in the response.")
    return {"text": text, "model": response.model, "stop_reason": response.stop_reason,
            "request_id": getattr(response, "_request_id", None),
            "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}}


def cached_call(request: dict, cache_dir: Path, refresh: bool = False, client=None) -> tuple[dict, bool]:
    """Return (result, from_cache). The parsed JSON answer is in result["data"]."""
    key = request_key(request)
    path = cache_dir / f"{key}.json"
    if path.exists() and not refresh:
        result = json.loads(path.read_text(encoding="utf-8"))
        from_cache = True
    else:
        result = call_claude(request, client)
        result["request_sha256"] = key
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        from_cache = False
    try:
        result["data"] = json.loads(result["text"])
    except json.JSONDecodeError as exc:
        raise LLMError("The response was not valid JSON.") from exc
    return result, from_cache


def nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def strict_object(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": sorted(properties), "properties": properties}


def missing_credentials(exc: Exception) -> bool:
    return type(exc).__name__ == "AnthropicError" or "api_key" in str(exc).lower()
