"""Freeze the state of the cluster at the moment an incident is detected, so the agent can be
evaluated on it later without the cluster (replay, CI, after the Azure credit ends).

An incident folder holds exactly what the agent's tools can see, nothing more:
  incident.json        alerts (agent input), ground truth (scoring only), timings
  metrics.om.gz        raw Prometheus samples of namespace shop + ALERTS over the last WINDOW_MIN
                       minutes, in OpenMetrics text (promtool turns it back into a TSDB block)
  kube/meta.json       recorded_at: the replay's "now"
  kube/<kind>.json     the read-only kinds of mcp_servers/kube (never secrets)
  kube/logs/<pod>[.previous].log

Raw samples are read with range-vector instant queries (metric[180m]), which return every stored
sample with its exact timestamp: no resampling, so rate() and histogram_quantile() give the same
numbers in replay as they did live.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import subprocess
import sys
import urllib.parse

PROM_PROXY = "/api/v1/namespaces/monitoring/services/prometheus:9090/proxy"
NAMESPACE = "shop"
WINDOW_MIN = 180  # = the agent's query_range limit, so no query reaches before the recording
SELECTORS = [f'{{namespace="{NAMESPACE}"}}', '{__name__=~"ALERTS|ALERTS_FOR_STATE"}']
KINDS = ["pods", "deployments", "replicasets", "events", "configmaps", "services", "endpoints", "networkpolicies"]
LOG_TAIL = 1000


def kubectl(*args: str, check: bool = True) -> str:
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")
    return r.stdout if r.returncode == 0 else ""


def prom(path: str, params: dict) -> dict:
    body = json.loads(kubectl("get", "--raw", f"{PROM_PROXY}{path}?{urllib.parse.urlencode(params, doseq=True)}"))
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {body.get('error')}")
    return body["data"]


# ---------- OpenMetrics encoding (pure functions, unit-tested) ----------

def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _value(v: str) -> str:
    f = float(v)
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    return v


def to_openmetrics(series: list[dict]) -> str:
    """Prometheus matrix results -> OpenMetrics text. Families stay contiguous (sorted by name)."""
    lines = []
    for s in sorted(series, key=lambda s: (s["metric"]["__name__"], sorted(s["metric"].items()))):
        labels = {k: v for k, v in s["metric"].items() if k != "__name__"}
        label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
        head = s["metric"]["__name__"] + (f"{{{label_text}}}" if label_text else "")
        for ts, v in s["values"]:
            lines.append(f"{head} {_value(v)} {float(ts):.3f}")
    return "\n".join(lines) + "\n# EOF\n"


# ---------- recording ----------

def record_metrics(at: float, window_min: int = WINDOW_MIN) -> tuple[str, int, int]:
    series = []
    for sel in SELECTORS:
        names = prom("/api/v1/label/__name__/values",
                     {"match[]": sel, "start": at - window_min * 60, "end": at})
        for name in sorted(names):
            # one metric at a time keeps each response small
            matchers = ",".join(m for m in (f'__name__="{name}"', sel[1:-1]) if not m.startswith("__name__=~"))
            data = prom("/api/v1/query", {"query": f"{{{matchers}}}[{window_min}m]", "time": at})
            series.extend(data["result"])
    unique = {json.dumps(s["metric"], sort_keys=True): s for s in series}  # a series can match both selectors
    samples = sum(len(s["values"]) for s in unique.values())
    return to_openmetrics(list(unique.values())), len(unique), samples


def record_kube(directory: str, at: float) -> int:
    os.makedirs(os.path.join(directory, "logs"), exist_ok=True)
    with open(os.path.join(directory, "meta.json"), "w") as f:
        json.dump({"recorded_at": at, "namespace": NAMESPACE}, f)
    pods = []
    for kind in KINDS:
        args = ["-n", NAMESPACE, "get", kind, "-o", "json"]
        if kind == "configmaps":
            args.append("--show-managed-fields")
        data = json.loads(kubectl(*args))
        with open(os.path.join(directory, f"{kind}.json"), "w") as f:
            json.dump({"items": data["items"]}, f, separators=(",", ":"))
        if kind == "pods":
            pods = data["items"]
    logs = 0
    for pod in pods:
        name = pod["metadata"]["name"]
        restarted = any(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))
        for previous in ([False, True] if restarted else [False]):
            text = kubectl("-n", NAMESPACE, "logs", name, f"--tail={LOG_TAIL}",
                           *(["--previous"] if previous else []), check=False)
            if text:
                fname = f"{name}.previous.log" if previous else f"{name}.log"
                with open(os.path.join(directory, "logs", fname), "w") as f:
                    f.write(text)
                logs += 1
    return logs


def prometheus_time() -> float:
    """The cluster's clock (from Prometheus), not this machine's: both sides of the replay agree."""
    return float(prom("/api/v1/query", {"query": "time()"})["result"][0])


def record(directory: str, incident: dict) -> str:
    at = prometheus_time()
    os.makedirs(directory, exist_ok=False)
    text, n_series, n_samples = record_metrics(at)
    with gzip.open(os.path.join(directory, "metrics.om.gz"), "wt", compresslevel=9) as f:
        f.write(text)
    n_logs = record_kube(os.path.join(directory, "kube"), at)
    incident = dict(incident, recorded_at=at, window_min=WINDOW_MIN,
                    recording={"series": n_series, "samples": n_samples, "log_files": n_logs})
    with open(os.path.join(directory, "incident.json"), "w") as f:
        json.dump(incident, f, indent=2)
    print(f"recorded {directory}: {n_series} series, {n_samples} samples, {n_logs} log files", file=sys.stderr)
    return directory
