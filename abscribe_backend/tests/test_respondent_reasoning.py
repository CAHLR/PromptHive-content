"""Respondent reasoning mode (QUESTION_DIFFICULTY_RESPONDENT_REASONING).

Off by default: prompts must be byte-identical to the answer-only prompts and
grading must be unchanged. On: brief working + one FINAL ANSWER line, 600
token cap, only the FINAL ANSWER line is graded, no line -> missing (NaN).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from abscribe_backend.services.question_difficulty import generate_responses as gr
from abscribe_backend.services.question_difficulty.final_answer import parse_final_answer
from abscribe_backend.services.question_difficulty.llm_providers import CompletionResult
from abscribe_backend.services.question_difficulty.respondents import (
    RESPONDENT_MODE_PERSONA,
    RESPONDENT_REASONING_ENV,
    RESPONDENT_REASONING_TOKEN_CAP,
    build_respondent_personas,
    respondent_reasoning_enabled,
)

FLAG = RESPONDENT_REASONING_ENV

NUMERIC_ITEM = {
    "item_id": "LimitLaw2_b1_e4",
    "stem": "Find the limit as x approaches 2 of (x^2 - 4)/(x - 2) - 6.",
    "answer_type": "numeric",
    "answer_key": "-2",
    "mc_choices": None,
}
ALGEBRA_ITEM = {
    "item_id": "alg1",
    "stem": "Solve for x: 2x + 1 = 7.",
    "answer_type": "algebra",
    "answer_key": "x=3",
    "mc_choices": None,
}
STRING_ITEM = {
    "item_id": "str1",
    "stem": "Are the lines y=2x and y=-x/2 parallel or perpendicular?",
    "answer_type": "string",
    "answer_key": "perpendicular",
    "mc_choices": None,
}
MC_ITEM = {
    "item_id": "mc1",
    "stem": "Which is prime?",
    "answer_type": "mc",
    "answer_key": "B",
    "mc_choices": "A, B, C, D",
}

# Today's answer-only prompts, written out literally so byte-identity is
# asserted against the text as it ships, not against the code under test.
EXPECTED_OFF_PROMPTS = {
    "numeric": (
        "Solve the following college algebra item.\n"
        "Question: Find the limit as x approaches 2 of (x^2 - 4)/(x - 2) - 6.\n"
        "\n"
        "Output format rules:\n"
        "- Output a single number or fraction (e.g., -4/7 or 0.5714). No words.\n"
        "Answer only. No prose."
    ),
    "algebra": (
        "Solve the following college algebra item.\n"
        "Question: Solve for x: 2x + 1 = 7.\n"
        "\n"
        "Output format rules:\n"
        "- Output a single simplified expression or equation (e.g., x=3 or y=4x-3). No words.\n"
        "Answer only. No prose."
    ),
    "string": (
        "Solve the following college algebra item.\n"
        "Question: Are the lines y=2x and y=-x/2 parallel or perpendicular?\n"
        "\n"
        "Output format rules:\n"
        "- Output a single word or short phrase (e.g., perpendicular). No extra punctuation.\n"
        "Answer only. No prose."
    ),
    "mc": (
        "Solve the following college algebra item.\n"
        "Question: Which is prime?\n"
        "\n"
        "Output format rules:\n"
        "- Output exactly one of: A, B, C, D\n"
        "Answer only. No prose."
    ),
}
ITEMS_BY_TYPE = {
    "numeric": NUMERIC_ITEM,
    "algebra": ALGEBRA_ITEM,
    "string": STRING_ITEM,
    "mc": MC_ITEM,
}

# Today's per-tier system prompts (before the respondent code suffix).
EXPECTED_OFF_TIER_PROMPTS = {
    "struggling": (
        "You are a college algebra student who often makes arithmetic mistakes "
        "and finds word problems difficult. Answer to the best of your ability."
    ),
    "developing": (
        "You are a college algebra student with partial understanding. "
        "You get easier problems right but struggle on multi-step items."
    ),
    "average": (
        "You are a typical college algebra student. "
        "You solve routine problems correctly but miss tricky ones."
    ),
    "strong": (
        "You are a strong college algebra student who usually gets problems right "
        "unless they require careful attention to detail."
    ),
    "expert": (
        "You are an excellent college algebra student who almost always finds "
        "the correct answer quickly."
    ),
}


class _unset_flag:
    """Context manager that removes the flag from the environment."""

    def __enter__(self):
        self._saved = os.environ.pop(FLAG, None)
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            os.environ[FLAG] = self._saved
        return False


class FlagTests(unittest.TestCase):
    def test_default_is_off(self):
        with _unset_flag():
            self.assertFalse(respondent_reasoning_enabled())
            self.assertIsNone(gr._respondent_max_tokens())

    def test_zero_is_off_one_is_on(self):
        with patch.dict(os.environ, {FLAG: "0"}):
            self.assertFalse(respondent_reasoning_enabled())
        with patch.dict(os.environ, {FLAG: "1"}):
            self.assertTrue(respondent_reasoning_enabled())
            self.assertEqual(gr._respondent_max_tokens(), 600)
            self.assertEqual(RESPONDENT_REASONING_TOKEN_CAP, 600)


class PromptBuilderOffTests(unittest.TestCase):
    """With the flag off (or unset) the prompts are byte-identical to today."""

    def test_user_prompts_unchanged_when_flag_unset(self):
        with _unset_flag():
            for answer_type, item in ITEMS_BY_TYPE.items():
                self.assertEqual(
                    gr.render_user_prompt(item), EXPECTED_OFF_PROMPTS[answer_type]
                )

    def test_user_prompts_unchanged_when_flag_zero(self):
        with patch.dict(os.environ, {FLAG: "0"}):
            for answer_type, item in ITEMS_BY_TYPE.items():
                self.assertEqual(
                    gr.render_user_prompt(item), EXPECTED_OFF_PROMPTS[answer_type]
                )

    def test_system_prompts_unchanged_when_flag_off(self):
        with _unset_flag():
            personas = build_respondent_personas(50, 42)
        self.assertEqual(
            {p.ability_tier for p in personas}, set(EXPECTED_OFF_TIER_PROMPTS)
        )
        for i, p in enumerate(personas, start=1):
            expected = (
                f"{EXPECTED_OFF_TIER_PROMPTS[p.ability_tier]} "
                f"Your persistent respondent code is R{i:03d}."
            )
            self.assertEqual(p.system_prompt, expected)


class PromptBuilderOnTests(unittest.TestCase):
    def test_user_prompt_asks_for_working_and_final_answer_line(self):
        with patch.dict(os.environ, {FLAG: "1"}):
            prompt = gr.render_user_prompt(NUMERIC_ITEM)
        self.assertIsInstance(prompt, str)
        lines = prompt.split("\n")
        self.assertEqual(lines[0], "Solve the following item.")
        self.assertEqual(lines[1], f"Question: {NUMERIC_ITEM['stem']}")
        self.assertIn("brief lines of working", prompt)
        self.assertIn("FINAL ANSWER: <answer>", prompt)
        # The item's required format still applies to the FINAL ANSWER line.
        self.assertIn(
            "- Output a single number or fraction (e.g., -4/7 or 0.5714). No words.",
            prompt,
        )
        self.assertNotIn("college algebra", prompt)
        self.assertNotIn("Answer only. No prose.", prompt)

    def test_user_prompt_keeps_per_type_format_rule(self):
        with patch.dict(os.environ, {FLAG: "1"}):
            alg = gr.render_user_prompt(ALGEBRA_ITEM)
            s = gr.render_user_prompt(STRING_ITEM)
            mc = gr.render_user_prompt(MC_ITEM)
        self.assertIn("single simplified expression or equation", alg)
        self.assertIn("single word or short phrase", s)
        self.assertIn("- Output exactly one of: A, B, C, D", mc)
        for p in (alg, s, mc):
            self.assertTrue(p.startswith("Solve the following item.\n"))
            self.assertIn("FINAL ANSWER: <answer>", p)

    def test_system_prompt_says_student(self):
        with patch.dict(os.environ, {FLAG: "1"}):
            personas = build_respondent_personas(50, 42)
        for i, p in enumerate(personas, start=1):
            self.assertNotIn("college algebra", p.system_prompt)
            # Only the two words change; everything else is as before.
            expected = (
                EXPECTED_OFF_TIER_PROMPTS[p.ability_tier].replace(
                    "college algebra student", "student"
                )
                + f" Your persistent respondent code is R{i:03d}."
            )
            self.assertEqual(p.system_prompt, expected)
        self.assertEqual(
            personas[0].system_prompt,
            "You are a student who often makes arithmetic mistakes and finds word "
            "problems difficult. Answer to the best of your ability. "
            "Your persistent respondent code is R001.",
        )


class FinalAnswerParsingTests(unittest.TestCase):
    def test_last_final_answer_line_wins(self):
        text = (
            "Factor: (x-2)(x+2)/(x-2) = x+2.\n"
            "FINAL ANSWER: 4\n"
            "Wait, subtract 6.\n"
            "FINAL ANSWER: -2"
        )
        self.assertEqual(parse_final_answer(text), "-2")

    def test_markdown_bold_and_case_tolerated(self):
        self.assertEqual(parse_final_answer("work\n**Final Answer:** 7/3"), "7/3")
        self.assertEqual(parse_final_answer("final answer:   x=3  "), "x=3")

    def test_no_line_is_none(self):
        self.assertIsNone(parse_final_answer("x+2 at 2 is 4, minus 6 is -2"))
        self.assertIsNone(parse_final_answer(""))
        self.assertIsNone(parse_final_answer("FINAL ANSWER:"))

    def test_answer_key_check_uses_the_same_extraction(self):
        from abscribe_backend.services.question_difficulty import answer_key_check as akc

        self.assertIs(akc.parse_final_answer, parse_final_answer)


class GradingPolicyTests(unittest.TestCase):
    def test_reasoning_grades_only_the_final_answer_line(self):
        text = "x+2 -> 4\n4 - 6 = -2\nFINAL ANSWER: -2"
        cleaned, score, rule, err = gr._grade_with_truncation_policy(
            NUMERIC_ITEM,
            text,
            finish_reason="stop",
            respondent_id="R001",
            item_id="LimitLaw2_b1_e4",
            reasoning=True,
        )
        self.assertEqual(score, 1)
        self.assertEqual(rule, "numeric_tolerance")
        self.assertIsNone(err)
        self.assertEqual(cleaned, "-2")

    def test_reasoning_wrong_final_answer_is_wrong(self):
        text = "x+2 -> 4\nFINAL ANSWER: 4"
        _cleaned, score, rule, _err = gr._grade_with_truncation_policy(
            NUMERIC_ITEM,
            text,
            finish_reason="stop",
            respondent_id="R001",
            item_id="LimitLaw2_b1_e4",
            reasoning=True,
        )
        self.assertEqual(score, 0)
        self.assertEqual(rule, "numeric_tolerance")

    def test_reasoning_working_without_final_line_is_not_graded_as_the_answer(self):
        # The working mentions the correct value but there is no FINAL ANSWER
        # line, so it is missing, never scored from the prose.
        text = "x+2 -> 4, then 4 - 6 = -2, so the limit is -2."
        with patch.object(gr, "_append_data_quality_event") as events, patch.object(
            gr, "_logger"
        ) as log:
            cleaned, score, rule, err = gr._grade_with_truncation_policy(
                NUMERIC_ITEM,
                text,
                finish_reason="stop",
                respondent_id="R001",
                item_id="LimitLaw2_b1_e4",
                reasoning=True,
            )
        self.assertTrue(math.isnan(score))
        self.assertEqual(rule, "no_final_answer_missing")
        self.assertEqual(cleaned, "")
        self.assertIsNone(err)
        self.assertTrue(log.warning.called)
        self.assertEqual(events.call_count, 1)
        event = events.call_args[0][0]
        self.assertEqual(event["type"], "no_final_answer_missing")
        self.assertEqual(event["raw_answer"], text)

    def test_reasoning_truncated_before_final_line_is_missing(self):
        text = "Let me factor the numerator: x^2 - 4 = (x-2)(x+2). Then"
        with patch.object(gr, "_append_data_quality_event"), patch.object(gr, "_logger"):
            _cleaned, score, rule, _err = gr._grade_with_truncation_policy(
                NUMERIC_ITEM,
                text,
                finish_reason="length",
                respondent_id="R001",
                item_id="LimitLaw2_b1_e4",
                reasoning=True,
            )
        self.assertTrue(math.isnan(score))
        self.assertEqual(rule, "no_final_answer_missing")

    def test_reasoning_truncated_after_wrong_final_line_still_truncated_missing(self):
        # Existing truncation rule still applies on top of the extracted line.
        text = "FINAL ANSWER: 4\nActually"
        with patch.object(gr, "_append_data_quality_event"), patch.object(gr, "_logger"):
            _cleaned, score, rule, _err = gr._grade_with_truncation_policy(
                NUMERIC_ITEM,
                text,
                finish_reason="length",
                respondent_id="R001",
                item_id="LimitLaw2_b1_e4",
                reasoning=True,
            )
        self.assertTrue(math.isnan(score))
        self.assertEqual(rule, "truncated_missing")

    def test_flag_off_bare_answer_graded_as_before(self):
        cleaned, score, rule, _err = gr._grade_with_truncation_policy(
            NUMERIC_ITEM,
            "-2",
            finish_reason="stop",
            respondent_id="R001",
            item_id="LimitLaw2_b1_e4",
        )
        self.assertEqual((cleaned, score, rule), ("-2", 1, "numeric_tolerance"))
        # With the flag off, multi-line text is graded exactly as the existing
        # grader grades raw text: no FINAL ANSWER extraction, no missing event.
        text = "4 - 6 = -2\nFINAL ANSWER: -2"
        with patch.object(gr, "_append_data_quality_event") as events:
            got = gr._grade_with_truncation_policy(
                NUMERIC_ITEM,
                text,
                finish_reason="stop",
                respondent_id="R001",
                item_id="LimitLaw2_b1_e4",
            )
        self.assertEqual(got, gr._grade_parsed_answer(NUMERIC_ITEM, text))
        self.assertNotEqual(got[2], "no_final_answer_missing")
        self.assertFalse(events.called)
        text = "I think the limit is 4, so FINAL ANSWER: 4 maybe"
        with patch.object(gr, "_append_data_quality_event") as events:
            got = gr._grade_with_truncation_policy(
                NUMERIC_ITEM,
                text,
                finish_reason="stop",
                respondent_id="R001",
                item_id="LimitLaw2_b1_e4",
            )
        self.assertEqual(got, gr._grade_parsed_answer(NUMERIC_ITEM, text))
        self.assertFalse(events.called)


class _ScriptedProvider:
    """Records every call and replies per respondent id.

    Keyed on respondent (not call order): ``asyncio.as_completed`` schedules
    the tasks from a set, so the order the fake is called in is not fixed.
    """

    def __init__(self, replies: dict[str, CompletionResult]):
        self.replies = dict(replies)
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies[kwargs["respondent_id"]]


class EndToEndRunTests(unittest.TestCase):
    """Production path (bounded async) with a scripted provider."""

    def _run(self, replies, env):
        provider = _ScriptedProvider(replies)
        with tempfile.TemporaryDirectory() as tmp:
            items_path = Path(tmp) / "items.csv"
            pd.DataFrame(
                [
                    {
                        "item_id": NUMERIC_ITEM["item_id"],
                        "stem": NUMERIC_ITEM["stem"],
                        "answer_type": "numeric",
                        "answer_key": "-2",
                        "mc_choices": "",
                    }
                ]
            ).to_csv(items_path, index=False)
            out_dir = Path(tmp) / "run"
            args = argparse.Namespace(
                items=str(items_path),
                n_respondents=2,
                temperature=1.0,
                seed=42,
                model_name="fake-model",
                out_dir=str(out_dir),
                respondent_mode=RESPONDENT_MODE_PERSONA,
                progress_callback=None,
            )
            with patch.dict(os.environ, env), patch.object(
                gr, "ask_provider_async", provider
            ), patch.object(gr, "_append_data_quality_event"), patch.object(
                gr, "_logger"
            ):
                asyncio.run(gr._generate_responses_async_bounded_impl(args, 4))
            import json

            long_df = pd.read_csv(out_dir / "responses_long.csv")
            run_meta = json.loads((out_dir / "run.json").read_text())
        return provider, long_df, run_meta

    def test_flag_on_caps_tokens_grades_final_line_and_records_flag(self):
        replies = {
            "R001": CompletionResult("x+2 = 4\n4 - 6 = -2\nFINAL ANSWER: -2", "stop"),
            "R002": CompletionResult("(x^2-4)/(x-2) = x+2 so the answer is 4", "stop"),
        }
        provider, long_df, run_meta = self._run(replies, {FLAG: "1"})
        self.assertEqual(len(provider.calls), 2)
        for call in provider.calls:
            self.assertEqual(call["max_tokens"], 600)
            self.assertTrue(call["prompt"].startswith("Solve the following item.\n"))
            self.assertIn("FINAL ANSWER: <answer>", call["prompt"])
            self.assertNotIn("college algebra", call["system_prompt"])
        by_resp = long_df.set_index("respondent_id")
        self.assertEqual(by_resp.loc["R001", "is_correct"], 1)
        self.assertEqual(by_resp.loc["R001", "rule_used"], "numeric_tolerance")
        self.assertTrue(math.isnan(by_resp.loc["R002", "is_correct"]))
        self.assertEqual(by_resp.loc["R002", "rule_used"], "no_final_answer_missing")
        self.assertIs(run_meta["respondent_reasoning"], True)
        self.assertEqual(run_meta["respondent_mode"], RESPONDENT_MODE_PERSONA)

    def test_flag_off_uses_default_cap_and_old_prompts(self):
        replies = {
            "R001": CompletionResult("-2", "stop"),
            "R002": CompletionResult("4", "stop"),
        }
        provider, long_df, run_meta = self._run(replies, {FLAG: "0"})
        for call in provider.calls:
            self.assertIsNone(call["max_tokens"])
            self.assertEqual(call["prompt"], EXPECTED_OFF_PROMPTS["numeric"])
            self.assertIn("college algebra student", call["system_prompt"])
        by_resp = long_df.set_index("respondent_id")
        self.assertEqual(by_resp.loc["R001", "is_correct"], 1)
        self.assertEqual(by_resp.loc["R002", "is_correct"], 0)
        self.assertIs(run_meta["respondent_reasoning"], False)


if __name__ == "__main__":
    unittest.main()
