"""End to end without a cluster or an LLM: replay Prometheus + kube fixture -> the three real MCP
servers over stdio -> the agent loop driven by a scripted model.

Proves the wiring the unit tests cannot: env reaches the server processes, PROM_AT pins time,
tool names route to the right server, outputs come back to the model.
Skipped unless the MCP SDK v2 and the prometheus/promtool binaries are installed (the VM and CI have them).
"""

import asyncio
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

import replay  # noqa: E402
from recorder import to_openmetrics  # noqa: E402
from test_loop import FakeLLM, GOOD, call  # noqa: E402
from test_replay import counter, have_binaries  # noqa: E402
from testutil import server_modules  # noqa: E402

def have_mcp_v2():
    try:
        from mcp.server import MCPServer  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_mcp_v2() and have_binaries(), "needs MCP SDK v2 and prometheus binaries")
class E2EReplayTest(unittest.TestCase):
    def setUp(self):
        kube_fixture, = server_modules("kube", "test_tools")  # its make_fixture() builds the kube folder
        self.dir = tempfile.mkdtemp()
        at = kube_fixture.NOW_TS
        os.makedirs(os.path.join(self.dir, "kube"))
        kube_fixture.make_fixture(os.path.join(self.dir, "kube"))

        def c(name, labels, per_s):
            s = counter(name, labels, per_s)
            s["values"] = [[at - 15 * (len(s["values"]) - 1 - i), v] for i, (_, v) in enumerate(s["values"])]
            return s
        series = [c("http_requests_total", {"namespace": "shop", "service": "orders", "code": "200"}, 1),
                  c("http_requests_total", {"namespace": "shop", "service": "orders", "code": "503"}, 3),
                  {"metric": {"__name__": "ALERTS", "alertname": "HighErrorRate", "alertstate": "firing",
                              "service": "orders", "severity": "page"},
                   "values": [[at - 15 * i, "1"] for i in range(5, -1, -1)]}]
        with gzip.open(os.path.join(self.dir, "metrics.om.gz"), "wt") as f:
            f.write(to_openmetrics(series))
        with open(os.path.join(self.dir, "incident.json"), "w") as f:
            json.dump({"scenario": "T001", "recorded_at": at,
                       "alerts": [{"labels": {"alertname": "HighErrorRate", "service": "orders"}}],
                       "ground_truth": {"component": "inventory", "fault": "crash_loop"}}, f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_agent_investigates_a_recorded_incident(self):
        from run import run_one
        llm = FakeLLM(
            [call("list_alerts", {}, 0), call("service_overview", {"service": "orders"}, 1)],
            [call("list_pods", {}, 2), call("get_logs", {"workload": "inventory", "previous": True}, 3)],
            [call("submit_diagnosis", dict(GOOD, fault="crash_loop"), 4)],
        )
        llm.provider, llm.model = "fake", "scripted"
        with open(os.path.join(self.dir, "incident.json")) as f:
            incident = json.load(f)
        record = asyncio.run(run_one(llm, incident, self.dir))

        outputs = {m["tool_call_id"]: m["content"] for m in llm.seen[2][0] if m["role"] == "tool"}
        self.assertIn('alertname="HighErrorRate"', outputs["c0"])
        self.assertIn("share of 5xx responses: 0.75", outputs["c1"])
        self.assertIn("CrashLoopBackOff", outputs["c2"])
        self.assertIn("inventroy", outputs["c3"])
        self.assertEqual(record["score"]["correct"], True)
        self.assertEqual(record["mode"], "replay")
        self.assertEqual([t["name"] for t in record["tool_calls"]],
                         ["list_alerts", "service_overview", "list_pods", "get_logs"])
        self.assertFalse(any(t["error"] for t in record["tool_calls"]))


if __name__ == "__main__":
    unittest.main()
