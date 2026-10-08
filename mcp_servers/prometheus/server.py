"""Read-only Prometheus MCP server for IncidentPilot (thin adapter over tools.py).

Env:
  PROM_URL  Prometheus base URL (default http://localhost:9090)
  PROM_AT   optional Unix time to pin "now" (replays of a recorded incident)
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from mcp.server import MCPServer  # noqa: E402

import tools  # noqa: E402
from backend import Backend, LiveBackend  # noqa: E402

# httpx logs every request at INFO level; keep only warnings so stderr stays readable.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer("prometheus")
_at = os.environ.get("PROM_AT")
backend: Backend = LiveBackend(os.environ.get("PROM_URL", "http://localhost:9090"), at=float(_at) if _at else None)


@mcp.tool()
def list_alerts() -> str:
    """List the alerts firing right now, with their labels (alertname, service, severity).
    Call this first in every investigation: it tells you which symptom woke you up.
    An alert names the service where the symptom is seen, which is not always the root cause."""
    return tools.list_alerts(backend)


@mcp.tool()
def query_instant(promql: str) -> str:
    """Run a PromQL query at the current time and return one value per matching series.
    Use it to check a precise number once you suspect something, for example
    sum by (service) (rate(http_requests_total{code=~"5.."}[1m])).
    For "is it getting worse / was it normal before?" use query_range instead.
    For a first look at a service, prefer service_overview."""
    return tools.query_instant(backend, promql)


@mcp.tool()
def query_range(promql: str, minutes: int = 30, step_seconds: int = 60) -> str:
    """Run a PromQL query over a past time window and return, per matching series, a summary
    (first, last, min, max, trend: flat/rising/falling/fluctuating) plus a few sampled points.
    Use it to see how a metric evolved: is it getting worse, was it normal before, is memory climbing?
    minutes = how far back to look (1 to 180, default 30); step_seconds = spacing between points
    (default 60, minimum 15, raised automatically for long windows).
    Example: promql='sum by (service) (rate(http_requests_total{code=~"5.."}[2m]))', minutes=15.
    For the value right now use query_instant; for a first look at a service prefer service_overview."""
    return tools.query_range(backend, promql, minutes, step_seconds)


@mcp.tool()
def service_overview(service: str) -> str:
    """One-call health summary of a shop service: traffic, error share, p95 latency,
    calls to its upstream by outcome, CPU, memory, restarts and ready replicas.
    Use it first on the alerting service, then on the services it depends on
    (gateway calls orders, orders calls inventory). service is a name like "orders"."""
    return tools.service_overview(backend, service)


if __name__ == "__main__":
    mcp.run()
