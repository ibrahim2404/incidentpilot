"""Which models can this key actually use right now, with tool calling, and how fast?

  python agent/probe.py                    # every provider with a key in .env
  python agent/probe.py gemini-3.5-flash   # only these models (provider guessed from the name)

For each model: one tiny request that must come back as a tool call. Prints status and latency.
Run it before an eval: the eval must use one model from start to end.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from llm import PROVIDERS  # noqa: E402
from run import load_env  # noqa: E402

CANDIDATES = {
    "gemini": ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"],
    "groq": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
}
TOOL = {"type": "function", "function": {
    "name": "submit_diagnosis", "description": "Submit the diagnosis.",
    "parameters": {"type": "object", "properties": {"component": {"type": "string", "enum": ["orders", "inventory"]}},
                   "required": ["component"]}}}
MESSAGES = [{"role": "user", "content": "Orders calls inventory; inventory has 0 ready replicas and orders "
                                        "logs connection refused to inventory. Submit the root-cause component."}]


async def probe(client, model: str) -> str:
    t0 = time.monotonic()
    try:
        r = await client.chat.completions.create(model=model, messages=MESSAGES, tools=[TOOL],
                                                 tool_choice="auto", temperature=0)
    except Exception as e:
        code = getattr(e, "status_code", "")
        return f"FAIL {code} {type(e).__name__}: {str(e)[:120]}"
    calls = r.choices[0].message.tool_calls or []
    took = time.monotonic() - t0
    if not calls:
        return f"NO TOOL CALL ({took:.1f}s)"
    return f"OK   {took:.1f}s  {calls[0].function.name}({calls[0].function.arguments})"


async def main() -> None:
    load_env(os.path.join(os.path.dirname(HERE), ".env"))
    from openai import AsyncOpenAI

    only = sys.argv[1:]
    for provider, cfg in PROVIDERS.items():
        key = os.environ.get(cfg["key_env"])
        if not key:
            print(f"{provider}: no {cfg['key_env']} in .env, skipped")
            continue
        client = AsyncOpenAI(base_url=cfg["base_url"], api_key=key, max_retries=0, timeout=60)
        try:
            listed = sorted(m.id.removeprefix("models/") for m in (await client.models.list()).data)
        except Exception as e:
            listed = []
            print(f"{provider}: could not list models ({type(e).__name__})")
        models = [m for m in only if (provider == "groq") == ("/" in m)] if only else CANDIDATES[provider]
        print(f"\n{provider}: {len(listed)} models listed for this key")
        for m in models:
            note = "" if not listed or m in listed else "  (not in the list)"
            print(f"  {m:28s} {await probe(client, m)}{note}")


if __name__ == "__main__":
    asyncio.run(main())
