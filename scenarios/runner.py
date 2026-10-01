"""Failure-injection runner.

  python scenarios/runner.py list
  python scenarios/runner.py validate
  python scenarios/runner.py run S001 [--keep]   # inject -> wait for alert -> save incident -> revert
  python scenarios/runner.py inject S001 | revert S001
  python scenarios/runner.py alerts              # currently firing alerts

`run` writes runs/<id>-<timestamp>.json with the firing alerts (the agent's future input)
and the ground truth (used only for scoring). Requires kubectl on PATH.
"""

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAULTS = {"memory_leak", "crash_loop", "bad_config", "dependency_down", "cpu_saturation"}
COMPONENTS = {"gateway", "orders", "inventory"}
PROM_PROXY = "/api/v1/namespaces/monitoring/services/prometheus:9090/proxy"


def load_all():
    out = {}
    for path in sorted(glob.glob(os.path.join(HERE, "S*.yaml"))):
        with open(path) as f:
            s = yaml.safe_load(f)
        s["_path"] = path
        out[s["id"]] = s
    return out


def validate(s):
    errs = []
    for key in ("id", "title", "ground_truth", "inject", "expect_alerts", "timeout_s", "revert"):
        if key not in s:
            errs.append(f"missing {key}")
    gt = s.get("ground_truth", {})
    if gt.get("component") not in COMPONENTS:
        errs.append(f"ground_truth.component must be one of {sorted(COMPONENTS)}")
    if gt.get("fault") not in FAULTS:
        errs.append(f"ground_truth.fault must be one of {sorted(FAULTS)}")
    for step in s.get("inject", []) + s.get("revert", []):
        if not isinstance(step, list) or not all(isinstance(a, str) for a in step):
            errs.append(f"step must be a list of strings: {step!r}")
    if not os.path.basename(s.get("_path", "")).startswith(s.get("id", "?")):
        errs.append("file name must start with the scenario id")
    return errs


def kubectl(args, capture=False, check=True):
    cmd = ["kubectl", *args]
    print("+ " + " ".join(cmd), file=sys.stderr)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr or ''}")
    return r.stdout if capture else None


def firing_alerts():
    raw = kubectl(["get", "--raw", f"{PROM_PROXY}/api/v1/alerts"], capture=True)
    alerts = json.loads(raw)["data"]["alerts"]
    return [a for a in alerts if a.get("state") == "firing"]


def wait_for(predicate, timeout_s, what, interval=10):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        print(f"  waiting for {what} ...", file=sys.stderr)
        time.sleep(interval)
    return None


def cmd_run(s, keep=False):
    names = lambda: {a["labels"]["alertname"] for a in firing_alerts()}
    if names():
        raise SystemExit(f"cluster not healthy before injection, firing: {sorted(names())}")
    started = dt.datetime.now(dt.timezone.utc)
    for step in s["inject"]:
        kubectl(step)
    expected = set(s["expect_alerts"])
    ok = wait_for(lambda: expected <= names(), s["timeout_s"], f"alerts {sorted(expected)}")
    alerts = firing_alerts()
    record = {
        "scenario": s["id"],
        "started_at": started.isoformat(),
        "detected": bool(ok),
        "seconds_to_alert": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds()),
        "alerts": alerts,                  # agent input
        "ground_truth": s["ground_truth"],  # scoring only, never given to the agent
    }
    os.makedirs(os.path.join(ROOT, "runs"), exist_ok=True)
    out = os.path.join(ROOT, "runs", f"{s['id']}-{started.strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(out, "w") as f:
        json.dump(record, f, indent=2)
    print(f"saved {out}", file=sys.stderr)
    if not keep:
        for step in s["revert"]:
            kubectl(step)
        cleared = wait_for(lambda: not names() or None, 360, "alerts to clear", interval=15)
        if not cleared and names():
            print(f"warning: still firing after revert: {sorted(names())}", file=sys.stderr)
    if not ok:
        raise SystemExit(f"{s['id']}: expected alerts {sorted(expected)} did not fire in {s['timeout_s']}s")
    print(f"{s['id']}: detected in {record['seconds_to_alert']}s")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["list", "validate", "run", "inject", "revert", "alerts"])
    p.add_argument("scenario", nargs="?")
    p.add_argument("--keep", action="store_true", help="leave the fault in place after run")
    a = p.parse_args(argv)
    scenarios = load_all()

    if a.command == "list":
        for sid, s in scenarios.items():
            print(f"{sid}  {s['title']}")
        return
    if a.command == "validate":
        bad = {sid: validate(s) for sid, s in scenarios.items()}
        bad = {k: v for k, v in bad.items() if v}
        for sid, errs in bad.items():
            print(f"{sid}: {'; '.join(errs)}")
        if bad:
            raise SystemExit(1)
        print(f"{len(scenarios)} scenarios valid")
        return
    if a.command == "alerts":
        print(json.dumps(firing_alerts(), indent=2))
        return
    if a.scenario not in scenarios:
        raise SystemExit(f"unknown scenario {a.scenario!r}; try `list`")
    s = scenarios[a.scenario]
    if a.command == "run":
        cmd_run(s, keep=a.keep)
    else:
        for step in s[a.command]:
            kubectl(step)


if __name__ == "__main__":
    main()
