"""Loop tests with a scripted fake LLM: no network, no cluster, no API key."""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loop import MAX_TOOL_CHARS, investigate, score  # noqa: E402
from prompts import user_message  # noqa: E402

GOOD = {"component": "inventory", "fault": "dependency_down", "confidence": 0.9,
        "evidence": ["inventory ready replicas 0/1"], "proposed_action": "scale_up"}


def call(name, args, i=0):
    return {"id": f"c{i}", "name": name, "arguments": json.dumps(args)}


class FakeLLM:
    """Returns the scripted replies in order and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    async def __call__(self, messages, tools):
        self.seen.append(([dict(m) for m in messages], tools))
        reply = self.replies.pop(0)
        return {"content": None, "tool_calls": reply, "usage": {"prompt_tokens": 100, "completion_tokens": 10}} \
            if isinstance(reply, list) else {"content": reply, "tool_calls": [], "usage": {}}


class FakeTools:
    def __init__(self, output="ok"):
        self.calls = []
        self.output = output

    async def __call__(self, name, args):
        self.calls.append((name, args))
        return self.output


def run(llm, tools=None, **kw):
    tools = tools or FakeTools()
    return asyncio.run(investigate("Alerts firing: x", [], llm, tools, **kw)), tools


class LoopTest(unittest.TestCase):
    def test_tool_then_submit(self):
        llm = FakeLLM([call("service_overview", {"service": "orders"})], [call("submit_diagnosis", GOOD, 1)])
        r, tools = run(llm)
        self.assertEqual(r.diagnosis, GOOD)
        self.assertEqual(r.stop_reason, "submitted")
        self.assertEqual(tools.calls, [("service_overview", {"service": "orders"})])
        self.assertEqual(r.prompt_tokens, 200)
        # the tool result went back to the model, linked to its call id
        last = llm.seen[1][0][-1]
        self.assertEqual((last["role"], last["tool_call_id"], last["content"]), ("tool", "c0", "ok"))

    def test_provider_extra_is_echoed_back(self):
        signed = dict(call("list_pods", {}), extra={"extra_content": {"google": {"thought_signature": "sig=="}}})
        llm = FakeLLM([signed], [call("submit_diagnosis", GOOD, 1)])
        run(llm)
        sent = llm.seen[1][0][-2]["tool_calls"][0]  # the assistant turn as sent back to the model
        self.assertEqual(sent["extra_content"], {"google": {"thought_signature": "sig=="}})
        self.assertEqual((sent["id"], sent["function"]["name"]), ("c0", "list_pods"))

    def test_submit_tool_is_offered(self):
        llm = FakeLLM([call("submit_diagnosis", GOOD)])
        run(llm)
        self.assertIn("submit_diagnosis", [t["function"]["name"] for t in llm.seen[0][1]])

    def test_invalid_diagnosis_is_rejected_and_retried(self):
        bad = dict(GOOD, fault="network_gremlins")
        llm = FakeLLM([call("submit_diagnosis", bad)], [call("submit_diagnosis", GOOD, 1)])
        r, _ = run(llm)
        self.assertEqual(r.diagnosis, GOOD)
        self.assertIn("fault must be one of", llm.seen[1][0][-1]["content"])

    def test_text_reply_gets_nudged(self):
        llm = FakeLLM("I think it is inventory.", [call("submit_diagnosis", GOOD)])
        r, _ = run(llm)
        self.assertEqual(r.diagnosis, GOOD)
        self.assertEqual(llm.seen[1][0][-1]["role"], "user")

    def test_step_budget(self):
        llm = FakeLLM(*[[call("list_pods", {}, i)] for i in range(3)])
        r, tools = run(llm, max_steps=3)
        self.assertIsNone(r.diagnosis)
        self.assertEqual(r.stop_reason, "max_steps")
        self.assertEqual(len(tools.calls), 3)

    def test_bad_json_arguments(self):
        broken = {"id": "c0", "name": "list_pods", "arguments": "{not json"}
        llm = FakeLLM([broken], [call("submit_diagnosis", GOOD, 1)])
        r, tools = run(llm)
        self.assertEqual(tools.calls, [])
        self.assertIn("not valid JSON", llm.seen[1][0][-1]["content"])

    def test_long_tool_output_truncated(self):
        llm = FakeLLM([call("get_logs", {"workload": "orders"})], [call("submit_diagnosis", GOOD, 1)])
        run(llm, FakeTools("x" * 10_000))
        sent = llm.seen[1][0][-1]["content"]
        self.assertLess(len(sent), MAX_TOOL_CHARS + 50)
        self.assertIn("truncated", sent)


class FailingLLM:
    async def __call__(self, messages, tools):
        raise RuntimeError("quota exhausted")


class ErrorTest(unittest.TestCase):
    def test_llm_error_is_recorded_not_raised(self):
        r, _ = run(FailingLLM())
        self.assertIsNone(r.diagnosis)
        self.assertTrue(r.stop_reason.startswith("llm_error: RuntimeError: quota exhausted"))


class ScoreTest(unittest.TestCase):
    truth = {"component": "inventory", "fault": "crash_loop", "also_accept": ["bad_config"]}

    def test_exact_and_also_accept(self):
        self.assertTrue(score({"component": "inventory", "fault": "crash_loop"}, self.truth)["correct"])
        self.assertTrue(score({"component": "inventory", "fault": "bad_config"}, self.truth)["correct"])

    def test_wrong_component_or_none(self):
        self.assertFalse(score({"component": "orders", "fault": "crash_loop"}, self.truth)["correct"])
        self.assertFalse(score(None, self.truth)["correct"])


class PromptTest(unittest.TestCase):
    def test_alert_message_has_no_ground_truth(self):
        alerts = [{"labels": {"alertname": "HighErrorRate", "service": "gateway", "severity": "page"},
                   "annotations": {"summary": "gateway 5xx above 20%"}}]
        text = user_message(alerts)
        self.assertIn("HighErrorRate on service=gateway", text)
        self.assertNotIn("inventory", text)


if __name__ == "__main__":
    unittest.main()
