"""Truncated incorrect answers are recorded as missing (NaN), not wrong."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from abscribe_backend.services.question_difficulty.generate_responses import (
    _grade_with_truncation_policy,
    _maybe_missing_truncated,
    _unpack_completion,
)
from abscribe_backend.services.question_difficulty.llm_providers import CompletionResult


class TruncationMissingTests(unittest.TestCase):
    def test_unpack_completion_accepts_str_and_result(self):
        self.assertEqual(_unpack_completion("hi"), ("hi", None))
        self.assertEqual(
            _unpack_completion(CompletionResult("x", "length")),
            ("x", "length"),
        )

    def test_length_incorrect_becomes_nan(self):
        score, rule = _maybe_missing_truncated(
            0,
            "numeric_tolerance",
            finish_reason="length",
            respondent_id="R001",
            item_id="NEW_real5_2",
        )
        self.assertTrue(math.isnan(score))
        self.assertEqual(rule, "truncated_missing")

    def test_length_correct_stays_correct(self):
        score, rule = _maybe_missing_truncated(
            1,
            "numeric_tolerance",
            finish_reason="length",
            respondent_id="R001",
            item_id="NEW_real5_2",
        )
        self.assertEqual(score, 1)
        self.assertEqual(rule, "numeric_tolerance")

    def test_stop_incorrect_stays_incorrect(self):
        score, rule = _maybe_missing_truncated(
            0,
            "numeric_tolerance",
            finish_reason="stop",
            respondent_id="R001",
            item_id="NEW_real5_2",
        )
        self.assertEqual(score, 0)
        self.assertEqual(rule, "numeric_tolerance")

    def test_grade_policy_logs_missing(self):
        item = {
            "answer_type": "numeric",
            "answer_key": "132",
            "mc_choices": None,
        }
        with patch(
            "abscribe_backend.services.question_difficulty.generate_responses._logger"
        ) as log:
            cleaned, score, rule, _err = _grade_with_truncation_policy(
                item,
                "7(15) - 2((6 - 3) - 16) + 1\n= 105 -",
                finish_reason="length",
                respondent_id="R012",
                item_id="NEW_real5_2",
            )
            self.assertTrue(math.isnan(score))
            self.assertEqual(rule, "truncated_missing")
            self.assertTrue(log.warning.called)
            msg = log.warning.call_args[0][0]
            self.assertIn("truncated incorrect", msg)


if __name__ == "__main__":
    unittest.main()
