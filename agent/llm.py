"""OpenAI-compatible chat client for free-tier providers (Gemini, Groq), with retries on rate limits.

Env:
  LLM_PROVIDER  gemini (default) or groq
  LLM_MODEL     model id (default per provider below)
  GEMINI_API_KEY / GROQ_API_KEY
If LANGFUSE_PUBLIC_KEY is set, calls are traced in Langfuse (tokens, latency, inputs, outputs).
"""

from __future__ import annotations

import asyncio
import os

PROVIDERS = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_env": "GEMINI_API_KEY", "default_model": "gemini-3.5-flash"},
    "groq": {"base_url": "https://api.groq.com/openai/v1",
             "key_env": "GROQ_API_KEY", "default_model": "openai/gpt-oss-120b"},
}


# Free tiers fail in two transient ways: 429 (requests per minute) and 503 (model overloaded).
# Waiting is right for both; switching provider mid-investigation would mix two models in one result.
BACKOFF_S = [10, 20, 40, 60, 90]


def is_transient(e: Exception) -> bool:
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(e, APIStatusError) and (e.status_code == 429 or e.status_code >= 500)


def provider_extra(tool_call) -> dict:
    """Fields a provider adds to a tool call beyond the OpenAI schema, to send back unchanged.

    Gemini 3 puts a thought signature in extra_content.google.thought_signature and rejects the
    next request (400) if the call comes back without it: the model resumes its reasoning from it.
    """
    extra = getattr(tool_call, "model_extra", None) or {}
    return {k: v for k, v in extra.items()
            if k not in ("id", "type", "function", "index", "role", "content", "tool_calls") and v is not None}


def _client_class():
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        from langfuse.openai import AsyncOpenAI  # drop-in wrapper that traces every call
    else:
        from openai import AsyncOpenAI
    return AsyncOpenAI


class ChatLLM:
    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider = provider or os.environ.get("LLM_PROVIDER", "gemini")
        cfg = PROVIDERS[self.provider]
        key = os.environ.get(cfg["key_env"])
        if not key:
            raise SystemExit(f"{cfg['key_env']} is not set (see .env.example)")
        self.model = model or os.environ.get("LLM_MODEL", cfg["default_model"])
        self.client = _client_class()(base_url=cfg["base_url"], api_key=key, max_retries=0)

    async def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        for attempt in range(len(BACKOFF_S) + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=tools, tool_choice="auto", temperature=0)
                break
            except Exception as e:
                if not is_transient(e) or attempt == len(BACKOFF_S):
                    raise
                await asyncio.sleep(BACKOFF_S[attempt])
        msg = resp.choices[0].message
        usage = resp.usage
        return {
            "content": msg.content,
            "extra": provider_extra(msg),  # message-level provider fields, echoed back the same way
            "tool_calls": [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments,
                            "extra": provider_extra(c)}
                           for c in (msg.tool_calls or [])],
            "usage": {"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                      "completion_tokens": getattr(usage, "completion_tokens", 0) or 0},
        }
