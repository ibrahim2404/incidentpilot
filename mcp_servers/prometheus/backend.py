"""Access to Prometheus for the MCP tools.

The tools only talk to a Backend. LiveBackend asks a real Prometheus over HTTP.
For replays, the same class points at a Prometheus loaded with a snapshot of the
incident, and `at` pins "now" to the incident time.
"""

from __future__ import annotations

import time
from typing import Protocol

import httpx


class PrometheusError(Exception):
    """The message shown to the model when Prometheus refused the query or could not be reached."""


class Backend(Protocol):
    def now(self) -> float: ...
    def instant(self, promql: str) -> list[dict]: ...
    def range(self, promql: str, start: float, end: float, step: float) -> list[dict]: ...
    def alerts(self) -> list[dict]: ...


class LiveBackend:
    def __init__(self, base_url: str, timeout_s: float = 10.0, at: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.at = at  # None = real time

    def now(self) -> float:
        return self.at if self.at is not None else time.time()

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = httpx.get(f"{self.base_url}{path}", params=params, timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise PrometheusError(f"Prometheus unreachable: {e}") from e
        try:
            body = response.json()
        except ValueError:
            raise PrometheusError(f"Prometheus returned HTTP {response.status_code} without JSON") from None
        if body.get("status") != "success":
            raise PrometheusError(f"{body.get('errorType', 'error')}: {body.get('error', 'unknown error')}")
        return body["data"]

    def instant(self, promql: str) -> list[dict]:
        data = self._get("/api/v1/query", {"query": promql, "time": self.now()})
        if data["resultType"] == "scalar":  # e.g. "1 + 1" returns one number, not series
            return [{"metric": {}, "value": data["result"]}]
        return data["result"]

    def range(self, promql: str, start: float, end: float, step: float) -> list[dict]:
        data = self._get("/api/v1/query_range", {"query": promql, "start": start, "end": end, "step": step})
        return data["result"]

    def alerts(self) -> list[dict]:
        r=self.instant('ALERTS{alertstate="firing"}')
        return r