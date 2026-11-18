"""Unit tests for OpenAI rate-limit backoff helpers."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from abscribe_backend.services.question_difficulty import llm_providers as lp


class _FakeRateLimit(Exception):
    def __init__(self, retry_after=None, status_code=429):
        super().__init__("Rate limit exceeded")
        self.status_code = status_code
        headers = {}
        if retry_after is not None:
            headers["retry-after"] = str(retry_after)
        self.response = SimpleNamespace(headers=headers)


class RateLimitBackoffTests(unittest.TestCase):
    def test_is_rate_limit_by_status(self):
        self.assertTrue(lp._is_rate_limit_error(_FakeRateLimit()))
        self.assertFalse(lp._is_rate_limit_error(ValueError("nope")))

    def test_retry_after_honored(self):
        exc = _FakeRateLimit(retry_after=3.5)
        with patch.object(lp.random, "uniform", return_value=0.1):
            sleep_s = lp._backoff_sleep_seconds(0, exc)
        self.assertAlmostEqual(sleep_s, 3.6, places=5)

    def test_exponential_fallback_capped(self):
        exc = _FakeRateLimit(retry_after=None)
        with patch.object(lp, "_RATE_LIMIT_BASE_SLEEP_SEC", 1.0), patch.object(
            lp, "_RATE_LIMIT_MAX_SLEEP_SEC", 5.0
        ), patch.object(lp.random, "random", return_value=1.0):
            # attempt 10 → huge exp, should clamp to max
            sleep_s = lp._backoff_sleep_seconds(10, exc)
        self.assertEqual(sleep_s, 5.0)


if __name__ == "__main__":
    unittest.main()
