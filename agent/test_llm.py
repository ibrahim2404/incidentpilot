"""Pacing and retry-hint logic of the LLM client (no network, no openai package needed)."""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm import ChatLLM, retry_hint_s  # noqa: E402


class PacingTest(unittest.TestCase):
    def test_requests_are_spaced(self):
        llm = object.__new__(ChatLLM)  # skip __init__: no key, no client
        llm.min_interval_s, llm._last_start = 0.2, 0.0

        async def three():
            return [await llm._pace() for _ in range(3)]

        t0 = time.monotonic()
        waits = asyncio.run(three())
        self.assertGreaterEqual(time.monotonic() - t0, 0.38)
        self.assertEqual(waits[0], 0.0)
        self.assertGreater(waits[1], 0.1)

    def test_retry_hint(self):
        self.assertAlmostEqual(retry_hint_s(Exception("Quota exceeded. Please retry in 3.132052764s.")), 4.132, places=2)
        self.assertEqual(retry_hint_s(Exception("overloaded")), 0.0)


if __name__ == "__main__":
    unittest.main()
