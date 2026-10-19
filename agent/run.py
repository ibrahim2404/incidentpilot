"""Run the agent on the incident happening now, or on a recorded incident file.

  python agent/run.py                               # alerts firing now (live cluster)
  python agent/run.py --incident runs/S002-*.json   # alerts from a scenario run, scored against its ground truth

Writes results/<scenario-or-live>-<time>.json and prints the diagnosis.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from llm import ChatLLM  # noqa: E402
from loop import investigate, score  # noqa: E402
from mcp_bridge import MCPBridge  # noqa: E402
from prompts import user_message  # noqa: E402


def load_env(path: str) -> None:
    """Minimal .env loader: KEY=value lines, existing environment wins."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def traced(fn, name: str):
    """Wrap fn in a Langfuse trace when keys are configured; otherwise return it unchanged."""
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return fn
    from langfuse import observe
    return observe(name=name)(fn)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--incident", help="runs/<id>-<time>.json from scenarios/runner.py")
    p.add_argument("--model", help="override LLM_MODEL")
    a = p.parse_args()
    load_env(os.path.join(os.path.dirname(HERE), ".env"))

    incident = json.load(open(a.incident)) if a.incident else None
    llm = ChatLLM(model=a.model)

    async with MCPBridge() as bridge:
        if incident:
            alert_text = user_message(incident["alerts"])
        else:  # live: the alerts as the Prometheus tool renders them right now
            text = await bridge.call("list_alerts", {})
            if text.lower().startswith("no ") or text.startswith("ERROR"):
                sys.exit(f"Nothing to investigate: {text}")
            alert_text = f"Alerts firing:\n{text}\nInvestigate and submit your diagnosis."
        run = traced(investigate, "incident")
        result = await run(alert_text, bridge.tools, llm, bridge.call)

    record = {
        "scenario": incident["scenario"] if incident else "live",
        "provider": llm.provider, "model": llm.model,
        "diagnosis": result.diagnosis, "stop_reason": result.stop_reason,
        "steps": result.steps, "tool_calls": result.tool_calls,
        "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
        "seconds": round(result.seconds, 1),
    }
    if incident:
        record["ground_truth"] = incident["ground_truth"]
        record["score"] = score(result.diagnosis, incident["ground_truth"])
    os.makedirs("results", exist_ok=True)
    out = os.path.join("results", f"{record['scenario']}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json")
    json.dump(record, open(out, "w"), indent=2)

    print(json.dumps(result.diagnosis, indent=2))
    print(f"tools used: {[c['name'] for c in result.tool_calls]}")
    print(f"steps {result.steps}, tokens {result.prompt_tokens}+{result.completion_tokens}, {record['seconds']}s, saved {out}")
    if incident:
        print(f"score: {record['score']}  (truth: {incident['ground_truth']['component']}/{incident['ground_truth']['fault']})")
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        from langfuse import get_client
        get_client().flush()


if __name__ == "__main__":
    asyncio.run(main())
