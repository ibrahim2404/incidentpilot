"""Run the agent on one incident.

  python agent/run.py                                  # live: the alerts firing now
  python agent/run.py --incident runs/S002-<ts>.json   # live cluster, alerts + ground truth from a scenario run
  python agent/run.py --replay golden/S002-<ts>        # recorded incident, no cluster needed (eval, CI)

Writes results/<scenario>-<time>.json and prints the diagnosis and the score when ground truth is known.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from loop import investigate, score  # noqa: E402
from prompts import user_message  # noqa: E402
from replay import Replay  # noqa: E402


def load_env(path: str) -> None:
    """Minimal .env loader: KEY=value lines, existing environment wins."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip().strip('"'))


def traced(fn, name: str):
    """Wrap fn in a Langfuse trace when keys are configured; otherwise return it unchanged."""
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return fn
    from langfuse import observe
    return observe(name=name)(fn)


async def run_one(llm, incident: dict | None, replay_dir: str | None = None, label: str = "") -> dict:
    """One investigation. In replay mode the MCP servers are pointed at the recording."""
    from mcp_bridge import MCPBridge  # the MCP SDK is only needed when an investigation really runs

    ctx = Replay(replay_dir) if replay_dir else contextlib.nullcontext({})
    with ctx as env:
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)  # inherited by the MCP server processes
        try:
            async with MCPBridge() as bridge:
                if incident:
                    alert_text = user_message(incident["alerts"])
                else:  # live: the alerts as the Prometheus tool renders them right now
                    text = await bridge.call("list_alerts", {})
                    if text.startswith(("No alerts", "ERROR")):
                        raise SystemExit(f"Nothing to investigate: {text}")
                    alert_text = f"Alerts firing:\n{text}\nInvestigate and submit your diagnosis."
                result = await traced(investigate, f"incident {label}".strip())(
                    alert_text, bridge.tools, llm, bridge.call)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    record = {
        "scenario": incident.get("scenario", "live") if incident else "live",
        "mode": "replay" if replay_dir else "live",
        "incident": replay_dir,
        "provider": llm.provider, "model": llm.model,
        "diagnosis": result.diagnosis, "stop_reason": result.stop_reason,
        "steps": result.steps, "tool_calls": result.tool_calls,
        "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
        "seconds": round(result.seconds, 1),
    }
    if incident and "ground_truth" in incident:
        record["ground_truth"] = incident["ground_truth"]
        record["score"] = score(result.diagnosis, incident["ground_truth"])
    return record


async def main() -> None:
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group()
    src.add_argument("--incident", help="runs/<id>-<time>.json from scenarios/runner.py (live cluster)")
    src.add_argument("--replay", help="golden/<id>-<time>/ recorded incident (no cluster)")
    p.add_argument("--model", help="override LLM_MODEL")
    a = p.parse_args()
    load_env(os.path.join(ROOT, ".env"))
    from llm import ChatLLM

    incident = None
    if a.incident:
        with open(a.incident) as f:
            incident = json.load(f)
    elif a.replay:
        with open(os.path.join(a.replay, "incident.json")) as f:
            incident = json.load(f)

    llm = ChatLLM(model=a.model)
    record = await run_one(llm, incident, a.replay, label=os.path.basename((a.replay or a.incident or "live").rstrip("/")))

    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    out = os.path.join(ROOT, "results", f"{record['scenario']}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json")
    with open(out, "w") as f:
        json.dump(record, f, indent=2)

    print(json.dumps(record["diagnosis"], indent=2))
    print(f"stop: {record['stop_reason']}")
    print(f"tools used: {[c['name'] for c in record['tool_calls']]}")
    print(f"steps {record['steps']}, tokens {record['prompt_tokens']}+{record['completion_tokens']}, "
          f"{record['seconds']}s, saved {out}")
    if "score" in record:
        gt = record["ground_truth"]
        print(f"score: {record['score']}  (truth: {gt['component']}/{gt['fault']})")
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        from langfuse import get_client
        get_client().flush()


if __name__ == "__main__":
    asyncio.run(main())
