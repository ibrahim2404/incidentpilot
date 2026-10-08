"""Logic of the Prometheus tools, independent of MCP so it can be unit-tested with a fake backend.

server.py only registers these functions as MCP tools (thin adapter).
"""

from __future__ import annotations

import re

import render
from backend import Backend, PrometheusError

MAX_MINUTES = 180
MAX_POINTS_PER_QUERY = 240
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")

OVERVIEW = {
    "requests per second": 'sum(rate(http_requests_total{{service="{s}"}}[2m]))',
    "share of 5xx responses": 'sum(rate(http_requests_total{{service="{s}",code=~"5.."}}[2m])) / sum(rate(http_requests_total{{service="{s}"}}[2m]))',
    "p95 latency (seconds)": 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{service="{s}"}}[2m])))',
    "calls to upstream by outcome (per second)": 'sum by (upstream, outcome) (rate(upstream_requests_total{{service="{s}"}}[2m]))',
    "CPU used (cores, limit is 0.2)": 'sum(rate(container_cpu_usage_seconds_total{{namespace="shop",container="{s}"}}[2m]))',
    "memory used (MiB, limit is 96)": 'sum(container_memory_working_set_bytes{{namespace="shop",container="{s}"}}) / 1024 / 1024',
    "container restarts in the last 15 min": 'sum(increase(kube_pod_container_status_restarts_total{{namespace="shop",container="{s}"}}[15m]))',
    "last termination reason": 'kube_pod_container_status_last_terminated_reason{{namespace="shop",container="{s}"}} == 1',
    "ready replicas": 'kube_deployment_status_replicas_ready{{namespace="shop",deployment="{s}"}}',
}


def list_alerts(backend: Backend) -> str:
    try:
        alerts = backend.alerts()
    except PrometheusError as e:
        return f"ERROR: {e}"
    if not alerts:
        return "No alerts are firing."
    return "\n".join(render.labels({k: v for k, v in a["metric"].items() if k != "alertstate"}) for a in alerts)


def query_instant(backend: Backend, promql: str) -> str:
    try:
        return render.vector(backend.instant(promql))
    except PrometheusError as e:
        return f"ERROR: {e}. Fix the PromQL and try again."


def query_range(backend: Backend, promql: str, minutes: int = 30, step_seconds: int = 60) -> str:
    if not 1 <= minutes <= MAX_MINUTES:
        return f"ERROR: minutes must be between 1 and {MAX_MINUTES}."
    step = max(step_seconds, 15, (minutes * 60) // MAX_POINTS_PER_QUERY)
    end = backend.now()
    try:
        return render.matrix(backend.range(promql, end - minutes * 60, end, step))
    except PrometheusError as e:
        return f"ERROR: {e}. Fix the PromQL and try again."


def service_overview(backend: Backend, service: str) -> str:
    if not SERVICE_NAME.match(service):
        return "ERROR: service must be a lowercase name like orders, gateway or inventory."
    lines = [f"Overview of {service}:"]
    for title, template in OVERVIEW.items():
        try:
            result = backend.instant(template.format(s=service))
        except PrometheusError as e:
            lines.append(f"- {title}: query failed ({e})")
            continue
        if not result:
            lines.append(f"- {title}: no data")
        elif title == "last termination reason":
            reasons = sorted({s["metric"].get("reason", "?") for s in result})
            lines.append(f"- {title}: {', '.join(reasons)}")
        elif title.startswith("calls to upstream"):
            parts = [f"{s['metric'].get('upstream')}/{s['metric'].get('outcome')} {render.number(s['value'][1])}" for s in result]
            lines.append(f"- {title}: {'; '.join(parts)}")
        else:
            lines.append(f"- {title}: {render.number(result[0]['value'][1])}")
    return "\n".join(lines)
