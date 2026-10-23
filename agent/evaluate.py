"""Evaluate the agent on every recorded incident in golden/ (replay, no cluster).

  python agent/evaluate.py                       # all incidents, 3 runs each, model from .env
  python agent/evaluate.py --repeats 1 --only S002 --model gemini-3.8-flash
  python agent/evaluate.py --min-accuracy 0.8    # CI gate: exit 1 below the threshold

Each run is saved as results/eval-<model>/<incident>__r<k>.json. Runs already on disk are reused, so
an interrupted eval (quota, network) resumes where it stopped, and changing the agent means
starting a new results folder (--tag). summary.json / summary.md aggregate everything.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import math
import os
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)



def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval of a proportion; honest for small n, unlike +-1.96*sqrt(p(1-p)/n)."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def _rate(flags: list) -> float | None:
    """Share of True among the flags that apply (None = not applicable to that incident)."""
    known = [f for f in flags if f is not None]
    return round(sum(known) / len(known), 3) if known else None


def summarize(runs: list[dict]) -> dict:
    scored = [r for r in runs if "score" in r]
    correct = sum(r["score"]["correct"] for r in scored)
    lo, hi = wilson(correct, len(scored))
    by_repeat = defaultdict(list)
    for r in scored:
        by_repeat[r["repeat"]].append(r["score"]["correct"])
    per_repeat = [sum(v) / len(v) for v in by_repeat.values()]

    def group(key):
        out = defaultdict(lambda: [0, 0])
        for r in scored:
            out[key(r)][0] += r["score"]["correct"]
            out[key(r)][1] += 1
        return {k: {"correct": c, "runs": n, "accuracy": round(c / n, 3)} for k, (c, n) in sorted(out.items())}

    return {
        "runs": len(runs),
        "incidents": len({r["incident"] for r in runs}),
        "accuracy": round(correct / len(scored), 3) if scored else 0.0,
        "accuracy_ci95": [round(lo, 3), round(hi, 3)],
        "accuracy_std_across_repeats": round(statistics.pstdev(per_repeat), 3) if len(per_repeat) > 1 else 0.0,
        "remediation_accuracy": _rate([r["score"].get("action_ok") for r in scored]),
        "injection_followed": _rate([r["score"].get("injection_followed") for r in scored]),
        "component_accuracy": round(sum(r["score"]["component_ok"] for r in scored) / len(scored), 3) if scored else 0.0,
        "by_difficulty": group(lambda r: r.get("difficulty", "L1")),
        "by_fault": group(lambda r: r["ground_truth"]["fault"]),
        "no_diagnosis": sum(r["diagnosis"] is None for r in runs),
        "llm_errors": sum(r["stop_reason"].startswith("llm_error") for r in runs),
        "avg_tool_calls": round(statistics.mean(len(r["tool_calls"]) for r in runs), 2) if runs else 0,
        "avg_prompt_tokens": round(statistics.mean(r["prompt_tokens"] for r in runs)) if runs else 0,
        "avg_completion_tokens": round(statistics.mean(r["completion_tokens"] for r in runs)) if runs else 0,
        # latency without quota waits: what the agent would take on a paid tier
        "latency_p50_s": percentile([r.get("active_seconds", r["seconds"]) for r in runs], 0.5),
        "latency_p95_s": percentile([r.get("active_seconds", r["seconds"]) for r in runs], 0.95),
        "served_models": sorted({m for r in runs for m in r.get("served_models", [])}),
    }


def markdown(summary: dict, model: str, runs: list[dict]) -> str:
    s = summary
    lines = [f"# Eval: {model}", "",
             f"{s['incidents']} incidents, {s['runs']} runs. Root-cause accuracy **{s['accuracy']:.0%}** "
             f"(95% CI {s['accuracy_ci95'][0]:.0%}-{s['accuracy_ci95'][1]:.0%}, std across repeats "
             f"{s['accuracy_std_across_repeats']:.1%}), component only {s['component_accuracy']:.0%}, "
             f"remediation {'n/a' if s['remediation_accuracy'] is None else format(s['remediation_accuracy'], '.0%')}.", "",
             "| group | correct / runs | accuracy |", "|---|---|---|"]
    for title, table in (("difficulty", s["by_difficulty"]), ("fault", s["by_fault"])):
        for k, v in table.items():
            lines.append(f"| {title} {k} | {v['correct']} / {v['runs']} | {v['accuracy']:.0%} |")
    if len(s.get("served_models", [])) > 1:
        lines += ["", f"WARNING: answers came from several models {s['served_models']}: the provider "
                  "redirected the model name during the eval, results mix models."]
    lines += ["", f"Avg tool calls {s['avg_tool_calls']}, tokens {s['avg_prompt_tokens']} in / "
              f"{s['avg_completion_tokens']} out, latency p50 {s['latency_p50_s']}s p95 {s['latency_p95_s']}s, "
              f"no diagnosis {s['no_diagnosis']}, LLM errors {s['llm_errors']}.",
              "" if s["injection_followed"] is None else
              f"Prompt-injection runs where the agent followed the injected instruction: {s['injection_followed']:.0%}.",
              "", "## Misses", ""]
    for r in sorted(runs, key=lambda r: r["incident"]):
        if "score" in r and not r["score"]["correct"]:
            d = r["diagnosis"] or {}
            lines.append(f"- {os.path.basename(r['incident'])} r{r['repeat']}: said "
                         f"{d.get('component', '-')}/{d.get('fault', '-')}, truth "
                         f"{r['ground_truth']['component']}/{r['ground_truth']['fault']} ({r['stop_reason']})")
    return "\n".join(lines) + "\n"


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--golden", default=os.path.join(ROOT, "golden"))
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--only", help="comma-separated incident name prefixes, e.g. S002,S004")
    p.add_argument("--model")
    p.add_argument("--tag", default="", help="suffix of the results folder (new agent version = new tag)")
    p.add_argument("--min-accuracy", type=float)
    a = p.parse_args()
    from llm import ChatLLM
    from run import load_env, run_one  # needs the MCP SDK; kept out of module import for unit tests

    load_env(os.path.join(ROOT, ".env"))

    llm = ChatLLM(model=a.model)
    out_dir = os.path.join(ROOT, "results", f"eval-{llm.model.replace('/', '_')}{'-' + a.tag if a.tag else ''}")
    os.makedirs(out_dir, exist_ok=True)
    incidents = sorted(d for d in glob.glob(os.path.join(a.golden, "*")) if os.path.exists(os.path.join(d, "incident.json")))
    if a.only:
        prefixes = tuple(a.only.split(","))
        incidents = [d for d in incidents if os.path.basename(d).startswith(prefixes)]
    if not incidents:
        raise SystemExit(f"no recorded incidents in {a.golden}")

    runs = []
    for repeat in range(1, a.repeats + 1):  # repeat-major: a partial eval still covers every incident
        for d in incidents:
            name = os.path.basename(d.rstrip("/"))
            path = os.path.join(out_dir, f"{name}__r{repeat}.json")
            if os.path.exists(path):
                with open(path) as f:
                    runs.append(json.load(f))
                continue
            with open(os.path.join(d, "incident.json")) as f:
                incident = json.load(f)
            record = await run_one(llm, incident, d, label=f"{name} r{repeat}")
            record.update(incident=name, repeat=repeat)
            if not record["stop_reason"].startswith("llm_error"):  # errors are retried next time
                with open(path, "w") as f:
                    json.dump(record, f, indent=2)
            runs.append(record)
            mark = "ok " if record.get("score", {}).get("correct") else "MISS"
            print(f"{mark} {name} r{repeat}: {(record['diagnosis'] or {}).get('component')}/"
                  f"{(record['diagnosis'] or {}).get('fault')} in {len(record['tool_calls'])} tools, "
                  f"{record['seconds']}s [{record['stop_reason'][:60]}]", flush=True)

    summary = summarize(runs)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write(markdown(summary, llm.model, runs))
    print(open(os.path.join(out_dir, "summary.md")).read())
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        from langfuse import get_client
        get_client().flush()
    if a.min_accuracy is not None and summary["accuracy"] < a.min_accuracy:
        raise SystemExit(f"accuracy {summary['accuracy']} below the gate {a.min_accuracy}")


if __name__ == "__main__":
    asyncio.run(main())
