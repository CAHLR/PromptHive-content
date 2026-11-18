"""Tests for SSE progress helpers and Accept negotiation plumbing."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from abscribe_backend.services.question_difficulty.sse_progress import (
    make_progress_event,
    progress_message,
    sse_response_from_worker,
)


class TestSseProgressHelpers(unittest.TestCase):
    def test_progress_message_simulating(self):
        self.assertEqual(
            progress_message("simulating", 180, 360),
            "Simulating respondents 180/360",
        )

    def test_make_progress_event(self):
        event = make_progress_event(2, 10, "simulating")
        self.assertEqual(event["type"], "progress")
        self.assertEqual(event["done"], 2)
        self.assertEqual(event["total"], 10)
        self.assertEqual(event["message"], "Simulating respondents 2/10")

    def test_sse_worker_emits_progress_then_result(self):
        def run(progress_callback):
            progress_callback(1, 2, "simulating")
            progress_callback(2, 2, "computing")
            return {"ok": True, "n": 2}

        with patch(
            "abscribe_backend.services.question_difficulty.sse_progress.stream_with_context",
            side_effect=lambda gen: gen,
        ):
            response = sse_response_from_worker(run)
            chunks = list(response.response)
        text = "".join(
            c.decode() if isinstance(c, (bytes, bytearray)) else c for c in chunks
        )
        lines = [
            line[len("data: ") :]
            for line in text.split("\n")
            if line.startswith("data: ")
        ]
        events = [json.loads(line) for line in lines]
        self.assertEqual(events[0]["type"], "progress")
        self.assertEqual(events[0]["done"], 1)
        self.assertEqual(events[1]["phase"], "computing")
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["payload"]["ok"], True)

    def test_sse_worker_soft_failure_is_result(self):
        def run(_progress_callback):
            return {
                "questions": [],
                "difficultyFilter": {"calibrationStatus": "error", "error": "boom"},
            }

        with patch(
            "abscribe_backend.services.question_difficulty.sse_progress.stream_with_context",
            side_effect=lambda gen: gen,
        ):
            response = sse_response_from_worker(run)
            chunks = list(response.response)
        text = "".join(
            c.decode() if isinstance(c, (bytes, bytearray)) else c for c in chunks
        )
        events = [
            json.loads(line[len("data: ") :])
            for line in text.split("\n")
            if line.startswith("data: ")
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "result")
        self.assertEqual(
            events[0]["payload"]["difficultyFilter"]["calibrationStatus"], "error"
        )

    def test_sse_worker_unexpected_exception_is_error_event(self):
        def run(_progress_callback):
            raise RuntimeError("hard fail")

        with patch(
            "abscribe_backend.services.question_difficulty.sse_progress.stream_with_context",
            side_effect=lambda gen: gen,
        ):
            response = sse_response_from_worker(run)
            chunks = list(response.response)
        text = "".join(
            c.decode() if isinstance(c, (bytes, bytearray)) else c for c in chunks
        )
        events = [
            json.loads(line[len("data: ") :])
            for line in text.split("\n")
            if line.startswith("data: ")
        ]
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("hard fail", events[-1]["error"])


class TestAsyncBoundedProgressCallback(unittest.IsolatedAsyncioTestCase):
    async def test_as_completed_preserves_order_and_reports_progress(self):
        import argparse
        import asyncio
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch

        import pandas as pd

        from abscribe_backend.services.question_difficulty.generate_responses import (
            generate_responses_async_bounded,
        )

        progress = []

        def cb(done, total, phase):
            progress.append((done, total, phase))

        with TemporaryDirectory() as tmp:
            items_path = Path(tmp) / "items.csv"
            pd.DataFrame(
                [
                    {
                        "item_id": "A",
                        "stem": "1+1",
                        "answer_key": "2",
                        "answer_type": "numeric",
                        "mc_choices": "",
                    },
                    {
                        "item_id": "B",
                        "stem": "2+2",
                        "answer_key": "4",
                        "answer_type": "numeric",
                        "mc_choices": "",
                    },
                ]
            ).to_csv(items_path, index=False)
            out_dir = Path(tmp) / "run"
            args = argparse.Namespace(
                items=str(items_path),
                n_respondents=2,
                temperature=0.5,
                seed=42,
                model_name="gpt-4o",
                out_dir=str(out_dir),
                respondent_mode="persona",
                progress_callback=cb,
            )

            async def fake_ask(**kwargs):
                await asyncio.sleep(0.01)
                return "2"

            with patch(
                "abscribe_backend.services.question_difficulty.generate_responses.ask_provider_async",
                new_callable=AsyncMock,
                side_effect=fake_ask,
            ):
                await generate_responses_async_bounded(args, max_concurrency=2)

            self.assertEqual(progress[-1], (4, 4, "simulating"))
            self.assertEqual([p[0] for p in progress], [1, 2, 3, 4])
            self.assertTrue((out_dir / "responses.csv").is_file())


if __name__ == "__main__":
    unittest.main()
