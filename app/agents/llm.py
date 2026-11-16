"""OpenAI-compatible Chat Completions connector.

Works with OpenAI, Azure OpenAI (``.../openai/deployments/<name>`` base URL),
and self-hosted OpenAI-compatible servers such as vLLM or Ollama. Only the
``/chat/completions`` endpoint with function tools is used.

Tests (and offline demos) can register a transport in
``app.extensions["dagonweb_llm_transport"]``: a callable taking the request
payload and returning the decoded JSON response.
"""
from __future__ import annotations

from typing import Any, Callable

import requests
from flask import current_app

from .. import settings


class LLMError(RuntimeError):
    """Raised for configuration, transport, or provider errors."""


def is_enabled() -> bool:
    return bool(settings.get("llm_enabled")) and bool(str(settings.get("llm_model")).strip())


def _transport() -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    return current_app.extensions.get("dagonweb_llm_transport")


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Send one Chat Completions request and return the assistant message."""
    payload: dict[str, Any] = {
        "model": str(settings.get("llm_model")),
        "messages": messages,
        "temperature": float(settings.get("llm_temperature")),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    transport = _transport()
    if transport is not None:
        data = transport(payload)
    else:
        base_url = str(settings.get("llm_base_url") or "").rstrip("/")
        if not base_url:
            raise LLMError("The LLM base URL is not configured.")
        headers = {"Content-Type": "application/json"}
        api_key = str(settings.get("llm_api_key") or "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["api-key"] = api_key  # Azure OpenAI
        try:
            response = requests.post(f"{base_url}/chat/completions", json=payload, headers=headers, timeout=int(settings.get("llm_timeout")))
        except requests.RequestException as exc:
            raise LLMError(f"cannot reach {base_url}: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = response.text[:200]
            raise LLMError(f"HTTP {response.status_code}: {detail}".strip())
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("the provider returned invalid JSON") from exc
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("the provider response has no message") from exc
    if not isinstance(message, dict):
        raise LLMError("the provider response has no message")
    return message


def ping() -> str:
    """Return a short reply to prove the connector works."""
    message = chat([{"role": "user", "content": "Reply with the single word OK."}])
    return str(message.get("content") or "").strip()
