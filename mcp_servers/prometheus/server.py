"""Read-only Prometheus MCP server for IncidentPilot.

Env:
  PROM_URL  Prometheus base URL (default http://localhost:9090)
  PROM_AT   optional Unix time to pin "now" (replays of a recorded incident)
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from mcp.server import MCPServer  # noqa: E402

import render  # noqa: E402
from backend import Backend, LiveBackend, PrometheusError  # noqa: E402

MAX_MINUTES = 180
MAX_POINTS_PER_QUERY = 240
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")

mcp = MCPServer("prometheus")
_at = os.environ.get("PROM_AT")
backend: Backend = LiveBackend(os.environ.get("PROM_URL", "http://localhost:9090"), at=float(_at) if _at else None)


@mcp.tool()
def list_alerts() -> str:
    """List the alerts firing right now, with their labels (alertname, service, severity).
    Call this first in every investigation: it tells you which symptom woke you up.
    An alert names the service where the symptom is seen, which is not always the root cause."""
    try:
        alerts = backend.alerts()
    except PrometheusError as e:
        return f"ERROR: {e}"
    if not alerts:
        return "No alerts are firing."
    return "\n".join(render.labels({k: v for k, v in a["metric"].items() if k != "alertstate"}) for a in alerts)


@mcp.tool()
def query_instant(promql: str) -> str:
    """Run a PromQL query at the current time and return one value per matching series.
    Use it to check a precise number once you suspect something, for example
    sum by (service) (rate(http_requests_total{code=~"5.."}[1m])).
    For "is it getting worse / was it normal before?" use query_range instead.
    For a first look at a service, prefer service_overview."""
    try:
        return render.vector(backend.instant(promql))
    except PrometheusError as e:
        return f"ERROR: {e}. Fix the PromQL and try again."


@mcp.tool()
def query_range(promql: str, minutes: int = 30, step_seconds: int = 60) -> str:
    """Run a PromQL query over a past time window and return, per matching series, a summary
    (first, last, min, max, trend: flat/rising/falling/fluctuating) plus a few sampled points.
    Use it to see how a metric evolved: is it getting worse, was it normal before, is memory climbing?
    minutes = how far back to look (1 to 180, default 30); step_seconds = spacing between points
    (default 60, minimum 15, raised automatically for long windows).
    Example: promql='sum by (service) (rate(http_requests_total{code=~"5.."}[1m]))', minutes=15.
    For the value right now use query_instant; for a first look at a service prefer service_overview."""
    if not 1 <= minutes <= MAX_MINUTES:
        return f"ERROR: minutes must be between 1 and {MAX_MINUTES}."
    step = max(step_seconds, 15, (minutes * 60) // MAX_POINTS_PER_QUERY)
    end = backend.now()
    try:
        return render.matrix(backend.range(promql, end - minutes * 60, end, step))
    except PrometheusError as e:
        return f"ERROR: {e}. Fix the PromQL and try again."


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


@mcp.tool()
def service_overview(service: str) -> str:
    """One-call health summary of a shop service: traffic, error share, p95 latency,
    calls to its upstream by outcome, CPU, memory, restarts and ready replicas.
    Use it first on the alerting service, then on the services it depends on
    (gateway calls orders, orders calls inventory). service is a name like "orders"."""
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


if __name__ == "__main__":
    mcp.run()
