"""Round trip: recorder encoding -> promtool block -> replay Prometheus -> the real Prometheus tools.
Skipped when the prometheus/promtool binaries are absent (make replay-tools)."""

import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "scenarios")]

from recorder import to_openmetrics  # noqa: E402
import replay  # noqa: E402
from testutil import server_modules  # noqa: E402

AT = 1_760_000_000.0  # a past time: replays must not depend on the wall clock


def counter(name, labels, per_s, n=240):
    return {"metric": {"__name__": name, **labels},
            "values": [[AT - 15 * (n - 1 - i), str(per_s * 15 * i)] for i in range(n)]}


def have_binaries():
    try:
        replay._binary("prometheus"), replay._binary("promtool")
        return True
    except SystemExit:
        return False


class EncodingTest(unittest.TestCase):
    def test_escaping_special_values_and_eof(self):
        text = to_openmetrics([{"metric": {"__name__": "m", "msg": 'say "hi"\\\n'},
                                "values": [[1.5, "NaN"], [2, "+Inf"]]}])
        self.assertIn('m{msg="say \\"hi\\"\\\\\\n"} NaN 1.500', text)
        self.assertTrue(text.endswith("# EOF\n"))

    def test_families_are_contiguous(self):
        text = to_openmetrics([counter("b", {"x": "1"}, 1, 2), counter("a", {}, 1, 2), counter("b", {"x": "0"}, 1, 2)])
        names = [line.split("{")[0].split(" ")[0] for line in text.splitlines()[:-1]]
        self.assertEqual(names, ["a", "a", "b", "b", "b", "b"])


class RecorderQueryTest(unittest.TestCase):
    def test_one_query_per_metric_and_dedup(self):
        import recorder
        queries = []

        def fake_prom(path, params):
            if path.endswith("/values"):
                return ["ALERTS"] if "ALERTS" in params["match[]"] else ["ALERTS", "up"]
            queries.append(params["query"])
            name = params["query"].split('"')[1]
            return {"result": [{"metric": {"__name__": name, "namespace": "shop"}, "values": [[AT, "1"]]}]}

        original, recorder.prom = recorder.prom, fake_prom
        try:
            text, n_series, n_samples = recorder.record_metrics(AT, window_min=180)
        finally:
            recorder.prom = original
        self.assertEqual(queries, ['{__name__="ALERTS",namespace="shop"}[180m]',
                                   '{__name__="up",namespace="shop"}[180m]', '{__name__="ALERTS"}[180m]'])
        self.assertEqual((n_series, n_samples), (2, 2))  # ALERTS matched both selectors, kept once
        self.assertTrue(text.endswith("# EOF\n"))


@unittest.skipUnless(have_binaries(), "prometheus/promtool not installed")
class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        labels = {"namespace": "shop", "service": "gateway"}
        series = [
            counter("http_requests_total", {**labels, "code": "200"}, 3),
            counter("http_requests_total", {**labels, "code": "503"}, 1),
            {"metric": {"__name__": "ALERTS", "alertname": "HighErrorRate", "alertstate": "firing",
                        "service": "gateway", "severity": "page"},
             "values": [[AT - 15 * i, "1"] for i in range(10, -1, -1)]},
        ]
        with gzip.open(os.path.join(self.dir, "metrics.om.gz"), "wt") as f:
            f.write(to_openmetrics(series))
        os.makedirs(os.path.join(self.dir, "kube"))
        for path in ("kube/meta.json", "incident.json"):
            with open(os.path.join(self.dir, path), "w") as f:
                json.dump({"recorded_at": AT}, f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_tools_see_the_recorded_incident(self):
        tools, backend = server_modules("prometheus", "tools", "backend")
        LiveBackend = backend.LiveBackend
        with replay.Replay(self.dir) as env:
            b = LiveBackend(env["PROM_URL"], at=float(env["PROM_AT"]))
            self.assertIn('alertname="HighErrorRate"', tools.list_alerts(b))
            overview = tools.service_overview(b, "gateway")
            self.assertIn("requests per second: 4", overview)
            self.assertIn("share of 5xx responses: 0.25", overview)
            trend = tools.query_range(b, 'sum(rate(http_requests_total{code="503"}[2m]))', minutes=10)
            self.assertIn("first 1, last 1", trend)


if __name__ == "__main__":
    unittest.main()
