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
               "key_env": "GEMINI_API_KEY", "default_model": "gemini-2.5-flash"},
    "groq": {"base_url": "https://api.groq.com/openai/v1",
             "key_env": "GROQ_API_KEY", "default_model": "openai/gpt-oss-120b"},
}


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
        from openai import RateLimitError

        for attempt in range(5):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=tools, tool_choice="auto", temperature=0)
                break
            except RateLimitError:
                if attempt == 4:
                    raise
                await asyncio.sleep(15 * (attempt + 1))  # free tiers count requests per minute
        msg = resp.choices[0].message
        usage = resp.usage
        return {
            "content": msg.content,
            "tool_calls": [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                           for c in (msg.tool_calls or [])],
            "usage": {"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                      "completion_tokens": getattr(usage, "completion_tokens", 0) or 0},
        }
