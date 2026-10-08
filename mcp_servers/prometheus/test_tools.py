"""Unit tests of the Prometheus tools with a fake backend (no network, no MCP).

Run: python -m unittest discover -s mcp_servers/prometheus
"""

import unittest

import render
import tools
from backend import PrometheusError


class FakeBackend:
    """Answers from a dict {promql: result}; records every query it receives."""

    def __init__(self, answers=None, alerts=None, fail=None):
        self.answers = answers or {}
        self._alerts = alerts or []
        self.fail = fail
        self.queries = []

    def now(self):
        return 1_000_000.0

    def instant(self, promql):
        self.queries.append(promql)
        if self.fail:
            raise PrometheusError(self.fail)
        return self.answers.get(promql, [])

    def range(self, promql, start, end, step):
        self.queries.append((promql, start, end, step))
        if self.fail:
            raise PrometheusError(self.fail)
        return self.answers.get(promql, [])

    def alerts(self):
        return self._alerts


def series(value, **labels):
    return {"metric": labels, "value": [1_000_000.0, str(value)]}


class RenderTest(unittest.TestCase):
    def test_empty_vector_is_explicit(self):
        self.assertIn("No series matched", render.vector([]))

    def test_vector_is_capped(self):
        out = render.vector([series(i, pod=f"p{i}") for i in range(40)])
        self.assertEqual(out.count("\n"), render.MAX_SERIES)  # 15 lines + 1 "more series" line
        self.assertIn("25 more series not shown", out)

    def test_noisy_labels_dropped(self):
        out = render.labels({"container": "orders", "id": "/kubepods/very/long", "image": "x", "name": "abc"})
        self.assertEqual(out, '{container="orders"}')

    def test_matrix_summary_and_trend(self):
        values = [[1000 + 60 * i, str(16 + 5 * i)] for i in range(30)]
        out = render.matrix([{"metric": {"container": "orders"}, "values": values}])
        self.assertIn("first 16, last 161", out)
        self.assertIn("trend rising", out)
        self.assertLessEqual(out.split("points:")[1].count(","), render.MAX_POINTS)

    def test_flat_series(self):
        values = [[1000 + 60 * i, "16.2"] for i in range(10)]
        self.assertIn("trend flat", render.matrix([{"metric": {}, "values": values}]))


class ToolsTest(unittest.TestCase):
    def test_list_alerts_hides_alertstate(self):
        b = FakeBackend(alerts=[series(1, __name__="ALERTS", alertname="HighErrorRate", service="orders", alertstate="firing")])
        out = tools.list_alerts(b)
        self.assertIn('alertname="HighErrorRate"', out)
        self.assertNotIn("alertstate", out)

    def test_no_alerts(self):
        self.assertEqual(tools.list_alerts(FakeBackend()), "No alerts are firing.")

    def test_bad_promql_becomes_text(self):
        out = tools.query_instant(FakeBackend(fail="bad_data: parse error"), "sum(")
        self.assertTrue(out.startswith("ERROR: bad_data"))
        self.assertIn("Fix the PromQL", out)

    def test_range_limits(self):
        self.assertTrue(tools.query_range(FakeBackend(), "up", minutes=500).startswith("ERROR"))
        b = FakeBackend()
        tools.query_range(b, "up", minutes=180, step_seconds=1)
        _, start, end, step = b.queries[0]
        self.assertEqual(end - start, 180 * 60)
        self.assertLessEqual((end - start) / step, tools.MAX_POINTS_PER_QUERY)

    def test_service_name_injection_blocked(self):
        b = FakeBackend()
        out = tools.service_overview(b, 'orders"} or vector(1) #')
        self.assertTrue(out.startswith("ERROR"))
        self.assertEqual(b.queries, [])  # nothing was sent to Prometheus

    def test_overview_reads_upstream_outcomes(self):
        q = tools.OVERVIEW["calls to upstream by outcome (per second)"].format(s="orders")
        b = FakeBackend(answers={q: [series(0.8, upstream="inventory", outcome="connection_refused")]})
        out = tools.service_overview(b, "orders")
        self.assertIn("inventory/connection_refused 0.8", out)
        self.assertIn("requests per second: no data", out)


if __name__ == "__main__":
    unittest.main()
