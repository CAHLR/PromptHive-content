"""Timeout / connection retries must not become silent is_correct=0 answers."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from abscribe_backend.services.question_difficulty import llm_providers as lp
from abscribe_backend.services.question_difficulty.generate_responses import (
    _raise_llm_failure,
)
from abscribe_backend.services.question_difficulty.llm_providers import LLMCallError


class _FakeTimeout(Exception):
    """Stand-in for OpenAI APITimeoutError / asyncio timeout."""

    def __init__(self, msg="Request timed out"):
        super().__init__(msg)


class TimeoutRetryTests(unittest.TestCase):
    def test_timeout_is_retryable(self):
        self.assertTrue(lp._is_retryable_error(_FakeTimeout()))
        self.assertTrue(lp._is_retryable_error(asyncio.TimeoutError()))
        self.assertFalse(lp._is_retryable_error(ValueError("bad request")))

    def test_async_timeout_retries_then_succeeds(self):
        calls = {"n": 0}
        logged = []

        class _FakeCompletions:
            async def create(self, **kwargs):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise _FakeTimeout()
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=" 42 "),
                            finish_reason="stop",
                        )
                    ]
                )

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                self.chat = SimpleNamespace(completions=_FakeCompletions())

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), patch.object(
            lp, "_LLM_MAX_ATTEMPTS", 5
        ), patch.object(lp, "_LLM_RETRY_BUDGET_SEC", 900.0), patch.object(
            lp, "_LLM_TIMEOUT_SEC", 90.0
        ), patch.object(lp, "_LLM_WAIT_FOR_SEC", 95.0), patch.object(
            lp.asyncio, "sleep", new_callable=AsyncMock
        ), patch.object(
            lp, "_log_retry", side_effect=lambda **kw: logged.append(kw)
        ), patch(
            "openai.AsyncOpenAI", _FakeClient
        ):
            result = asyncio.run(
                lp._ask_openai_async(
                    prompt="q",
                    seed=1,
                    temperature=0.5,
                    model_name="gpt-4o",
                    system_prompt=None,
                    respondent_id="R009",
                    item_id="NEW_real2_1",
                )
            )
        self.assertEqual(result.text, "42")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(logged), 2)
        self.assertEqual(logged[0]["respondent_id"], "R009")
        self.assertEqual(logged[0]["item_id"], "NEW_real2_1")
        self.assertEqual(type(logged[0]["exc"]).__name__, "_FakeTimeout")

    def test_async_exhausted_timeout_raises_llm_call_error(self):
        class _AlwaysTimeout:
            async def create(self, **kwargs):
                raise _FakeTimeout("hung")

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                self.chat = SimpleNamespace(completions=_AlwaysTimeout())
                # Capture constructor kwargs for max_retries / timeout.
                _FakeClient.kwargs = kwargs

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), patch.object(
            lp, "_LLM_MAX_ATTEMPTS", 3
        ), patch.object(lp, "_LLM_RETRY_BUDGET_SEC", 900.0), patch.object(
            lp, "_LLM_TIMEOUT_SEC", 90.0
        ), patch.object(lp, "_LLM_WAIT_FOR_SEC", 95.0), patch.object(
            lp.asyncio, "sleep", new_callable=AsyncMock
        ), patch(
            "openai.AsyncOpenAI", _FakeClient
        ):
            with self.assertRaises(LLMCallError) as ctx:
                asyncio.run(
                    lp._ask_openai_async(
                        prompt="q",
                        seed=1,
                        temperature=0.5,
                        model_name="gpt-4o",
                        system_prompt=None,
                    )
                )
        self.assertIn("after 3 attempt", str(ctx.exception))
        self.assertEqual(_FakeClient.kwargs.get("max_retries"), 0)
        self.assertEqual(_FakeClient.kwargs.get("timeout"), 90.0)

    def test_raise_llm_failure_names_respondent_and_item(self):
        with self.assertRaises(LLMCallError) as ctx:
            _raise_llm_failure(
                respondent_id="R007",
                item_id="NEW_item_1",
                exc=_FakeTimeout("hung"),
            )
        msg = str(ctx.exception)
        self.assertIn("R007", msg)
        self.assertIn("NEW_item_1", msg)
        self.assertIn("Refusing to record", msg)

    def test_async_bounded_does_not_write_is_correct_zero_on_transport_failure(self):
        """Exhausted LLM failure must abort the run, not grade as wrong."""
        import argparse
        import tempfile
        from pathlib import Path

        import pandas as pd

        from abscribe_backend.services.question_difficulty.generate_responses import (
            generate_responses_async_bounded,
        )

        async def _boom(**kwargs):
            raise LLMCallError("OpenAI call failed after 3 attempt(s): hung")

        with tempfile.TemporaryDirectory() as tmp:
            items_path = Path(tmp) / "items.csv"
            pd.DataFrame(
                [
                    {
                        "item_id": "I1",
                        "stem": "1+1?",
                        "answer_key": "2",
                        "answer_type": "numeric",
                        "mc_choices": "",
                    }
                ]
            ).to_csv(items_path, index=False)
            args = argparse.Namespace(
                items=str(items_path),
                n_respondents=1,
                seed=42,
                temperature=0.5,
                model_name="gpt-4o",
                out_dir=str(Path(tmp) / "run"),
                respondent_mode="persona",
                progress_callback=None,
            )
            with patch(
                "abscribe_backend.services.question_difficulty.generate_responses.ask_provider_async",
                side_effect=_boom,
            ), patch(
                "abscribe_backend.services.question_difficulty.generate_responses.load_dotenv"
            ):
                with self.assertRaises(LLMCallError) as ctx:
                    asyncio.run(
                        generate_responses_async_bounded(args, max_concurrency=1)
                    )
            self.assertIn("R001", str(ctx.exception))
            self.assertIn("I1", str(ctx.exception))
            # No responses.csv written with a fake wrong answer.
            self.assertFalse((Path(tmp) / "run" / "responses.csv").exists())


if __name__ == "__main__":
    unittest.main()
