"""Tests for the answer-key check on generated OATutor rows."""

from __future__ import annotations

import copy
import os
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from abscribe_backend.models.oatutor_hints import OatutorQuestion
from abscribe_backend.services import event_logger
from abscribe_backend.services import chatgpt_service
from abscribe_backend.services.question_difficulty import answer_key_check as akc
from abscribe_backend.services.question_difficulty.difficulty_service import (
    CALIBRATION_CALIBRATED,
    CALIBRATION_INSUFFICIENT,
    _oatutor_questions_to_items_df,
    filter_questions_by_difficulty_range,
)
from abscribe_backend.services.question_difficulty import llm_providers
from abscribe_backend.services.question_difficulty.llm_providers import (
    CompletionResult,
)


def _problem(name="p1", body="Evaluate the integral of 2x^2-3x+1 from x=1 to 4."):
    return {
        "Problem Name": name,
        "Row Type": "problem",
        "Title": "",
        "Body Text": body,
        "Answer": "",
        "answerType": "",
        "mcChoices": "",
    }


def _step(name="p1", answer="11", answer_type="numeric", **extra):
    row = {
        "Problem Name": name,
        "Row Type": "step",
        "Title": "",
        "Body Text": "Give the value of the definite integral.",
        "Answer": answer,
        "answerType": answer_type,
        "mcChoices": "",
    }
    row.update(extra)
    return row


@asynccontextmanager
async def _no_client(**_kwargs):
    yield None


class FakeSolver:
    """Replaces ``answer_key_check.ask_llm_async``.

    ``script`` maps the item index (parsed from ``item_id`` ``keycheck_<i>``)
    to a list of per-call outcomes: a string final answer, ``None`` for a reply
    with no FINAL ANSWER line, ``TRUNCATED`` for a reply cut off at the token
    cap (``finish_reason="length"``, no FINAL ANSWER line), or an Exception
    instance to raise. Retries consume the next outcome for that item.
    """

    TRUNCATED = object()
    TRUNCATED_TEXT = (
        "1. **Factor the numerator** using synthetic division:\n"
        "   \\[\n   \\begin{array}{r|rrrr}\n   -1 & 1 & 2 & -1 & -2 \\\\\n"
        "   & & -1 & -1 & 2 \\\\\n   \\hline\n   & 1 & 1 & -2 & 0 \\\\\n"
        "   \\end{array}\n   \\]\n"
        "   The quotient is \\(x^2 + x - 2\\).\n\n"
        "3. **Factor Further:**\n   \\[\n   x^2 + x - 2 = (x + 2)(x - 1)\n   \\]\n\n"
        "4. **Rewrite the Expression:**\n   \\[\n   \\"
    )

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def __call__(self, prompt, seed, **kwargs):
        self.calls.append({"prompt": prompt, "seed": seed, **kwargs})
        idx = int(str(kwargs.get("item_id", "keycheck_0")).rsplit("_", 1)[1])
        outcomes = self.script[idx]
        outcome = outcomes[len([c for c in self.calls if c.get("item_id") == kwargs.get("item_id")]) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return CompletionResult(text="I am not sure how to finish this.", finish_reason="stop")
        if outcome is self.TRUNCATED:
            return CompletionResult(text=self.TRUNCATED_TEXT, finish_reason="length")
        return CompletionResult(
            text=f"Step 1: integrate.\nStep 2: evaluate.\nFINAL ANSWER: {outcome}",
            finish_reason="stop",
        )


def _run(questions, script, **kwargs):
    solver = FakeSolver(script)
    with mock.patch.object(akc, "ask_llm_async", solver), mock.patch.object(
        akc, "shared_async_openai_client", _no_client
    ):
        out, report = akc.verify_answer_keys(questions, model="fake-model", **kwargs)
    return out, report, solver


class VerifyAnswerKeysTests(unittest.TestCase):
    def test_verified_when_all_three_match(self):
        qs = [_problem(), _step(answer="7")]
        out, report, solver = _run(qs, {1: ["7", "7", "7"]})
        step = out[1]
        self.assertEqual(step["answerKeyCheck"], "verified")
        self.assertEqual(step["Answer"], "7")
        self.assertNotIn("answerKeyOriginal", step)
        self.assertEqual(step["answerKeySolves"], ["7", "7", "7"])
        self.assertEqual(step["answerKeyCheckModel"], "fake-model")
        self.assertEqual(report["verified"], 1)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(len(solver.calls), 3)

    def test_verified_when_two_of_three_match(self):
        qs = [_problem(), _step(answer="7")]
        out, report, _ = _run(qs, {1: ["7", "8", "7"]})
        self.assertEqual(out[1]["answerKeyCheck"], "verified")
        self.assertEqual(out[1]["Answer"], "7")
        self.assertEqual(report["verified"], 1)

    def test_labelled_key_m_equals_is_verified_not_corrected(self):
        qs = [_problem(), _step(answer="m=2", answer_type="algebra")]
        out, report, _ = _run(qs, {1: ["2", "2", "2"]})
        self.assertEqual(out[1]["answerKeyCheck"], "verified")
        self.assertEqual(out[1]["Answer"], "m=2")
        self.assertNotIn("answerKeyOriginal", out[1])
        self.assertEqual(report["verified"], 1)
        self.assertEqual(report["corrected"], 0)

    def test_answer_in_text_is_skipped_without_solving(self):
        leaked = "y=-2/3x+5/3"
        qs = [
            _problem(body="Write the line through (3,-2) with slope -2/3."),
            _step(
                answer=leaked,
                answer_type="algebra",
                **{"Body Text": "Using the point-slope formula. Simplifying, we get y=-2/3x+5/3."},
            ),
        ]
        out, report, solver = _run(qs, {})
        self.assertEqual(out[1]["answerKeyCheck"], "skipped")
        self.assertEqual(out[1]["answerKeyCheckReason"], "answer_in_text")
        self.assertEqual(out[1]["Answer"], leaked)
        self.assertEqual(solver.calls, [])
        self.assertEqual(report["skipped"], 1)
        self.assertTrue(akc.answer_appears_in_text(leaked, qs[1]["Body Text"]))
        self.assertFalse(akc.answer_appears_in_text("2", "Find x when 2x=10."))

    def test_string_key_is_verified_not_mapped_to_algebra(self):
        qs = [_problem(), _step(answer="Perpendicular", answer_type="string")]
        out, report, _ = _run(qs, {1: ["perpendicular", "perpendicular", "perpendicular"]})
        self.assertEqual(out[1]["answerKeyCheck"], "verified")
        self.assertEqual(out[1]["Answer"], "Perpendicular")
        self.assertEqual(akc._answer_type(qs[1]), "string")
        self.assertEqual(report["verified"], 1)
        self.assertEqual(report["corrected"], 0)

    def test_decide_form_only_difference_is_verified(self):
        status, corrected, reason = akc.decide("algebra", "m=2", ["2", "2", "2"])
        self.assertEqual(status, "verified")
        self.assertIsNone(corrected)
        self.assertIsNone(reason)

    def test_decide_real_value_change_is_still_corrected(self):
        status, corrected, reason = akc.decide("numeric", "3", ["-3", "-3", "-3"])
        self.assertEqual(status, "corrected")
        self.assertEqual(corrected, "-3")
        self.assertIsNone(reason)

    def test_corrected_when_solves_agree_and_none_match(self):
        qs = [_problem(), _step(answer="11")]
        out, report, _ = _run(qs, {1: ["7", "7", "7"]})
        step = out[1]
        self.assertEqual(step["answerKeyCheck"], "corrected")
        self.assertEqual(step["Answer"], "7")
        self.assertEqual(step["answerKeyOriginal"], "11")
        self.assertEqual(step["answerKeySolves"], ["7", "7", "7"])
        self.assertEqual(report["corrected"], 1)
        self.assertEqual(report["verified"], 0)

    def test_corrected_row_rerenders_answer_columns_and_verified_row_keeps_them(self):
        from abscribe_backend.models.oatutor_hints import _convert_rendered, _convert_tutoring

        rendered_old = _convert_rendered("25/3")[0]
        tutoring_old = _convert_tutoring("25/3")[0]
        rendered_ok = _convert_rendered("3")[0]
        tutoring_ok = _convert_tutoring("3")[0]
        qs = [
            _problem(),
            _step(answer="25/3", **{"Answer Rendered": rendered_old, "Answer Tutoring": tutoring_old}),
            _step(name="p1", answer="3", **{"Answer Rendered": rendered_ok, "Answer Tutoring": tutoring_ok}),
            _step(name="p1", answer="11"),  # no rendered columns on this row
        ]
        out, _, _ = _run(qs, {1: ["7", "7", "7"], 2: ["3", "3", "3"], 3: ["7", "7", "7"]})

        corrected = out[1]
        self.assertEqual(corrected["answerKeyCheck"], "corrected")
        self.assertEqual(corrected["Answer"], "7")
        self.assertEqual(corrected["Answer Rendered"], _convert_rendered("7")[0])
        self.assertEqual(corrected["Answer Tutoring"], _convert_tutoring("7")[0])
        self.assertNotEqual(corrected["Answer Rendered"], rendered_old)
        self.assertEqual(corrected["answerKeyOriginal"], "25/3")

        verified = out[2]
        self.assertEqual(verified["answerKeyCheck"], "verified")
        self.assertEqual(verified["Answer"], "3")
        self.assertEqual(verified["Answer Rendered"], rendered_ok)
        self.assertEqual(verified["Answer Tutoring"], tutoring_ok)

        # Rendered columns are only touched when already present on the row.
        bare = out[3]
        self.assertEqual(bare["answerKeyCheck"], "corrected")
        self.assertEqual(bare["Answer"], "7")
        self.assertNotIn("Answer Rendered", bare)
        self.assertNotIn("Answer Tutoring", bare)

    def test_corrected_answer_is_normalized_for_numeric(self):
        qs = [_problem(), _step(answer="11")]
        out, _, _ = _run(qs, {1: ["x = 7", "7", "7.0"]})
        self.assertEqual(out[1]["answerKeyCheck"], "corrected")
        self.assertEqual(out[1]["Answer"], "7")

    def test_unverified_when_solves_disagree(self):
        qs = [_problem(), _step(answer="11")]
        out, report, _ = _run(qs, {1: ["7", "8", "9"]})
        self.assertEqual(out[1]["answerKeyCheck"], "unverified")
        self.assertEqual(out[1]["Answer"], "11")
        self.assertNotIn("answerKeyOriginal", out[1])
        self.assertEqual(report["unverified"], 1)

    def test_unverified_when_only_one_matches(self):
        qs = [_problem(), _step(answer="7")]
        out, _, _ = _run(qs, {1: ["7", "8", "8"]})
        self.assertEqual(out[1]["answerKeyCheck"], "unverified")

    def test_unverified_after_two_call_errors_leaves_answer_unchanged(self):
        qs = [_problem(), _step(answer="11")]
        out, report, _ = _run(
            qs, {1: [TimeoutError("timed out"), "7", ConnectionError("boom")]}
        )
        step = out[1]
        self.assertEqual(step["answerKeyCheck"], "unverified")
        self.assertEqual(step["answerKeyCheckReason"], "insufficient_solves")
        self.assertEqual(step["Answer"], "11")
        self.assertEqual(step["answerKeySolves"], ["7"])
        self.assertEqual(report["unverified"], 1)
        self.assertEqual(report["skipped"], 0)
        # The unchecked key is held out of the Rasch run but the row stays visible.
        items_df, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(out)
        self.assertEqual(len(items_df), 0)
        self.assertIsNone(index_to_item_id[1])
        self.assertEqual(skip_reasons, {1: "answer_key_unverified"})

    def test_unverified_when_replies_have_no_final_answer_line(self):
        qs = [_problem(), _step(answer="11")]
        out, report, solver = _run(qs, {1: [None, None, "7"]})
        self.assertEqual(out[1]["answerKeyCheck"], "unverified")
        self.assertEqual(out[1]["answerKeyCheckReason"], "insufficient_solves")
        self.assertEqual(out[1]["Answer"], "11")
        self.assertEqual(report["unverified"], 1)
        # A clean stop with no FINAL ANSWER line is not retried.
        self.assertEqual(len(solver.calls), 3)

    def test_limitlaw2_b1_e4_truncated_solves_come_out_unverified_never_scored(self):
        # Real run 2026-10-21 03:34: key 0 on lim x->-1 (x^3+2x^2-x-2)/(x+1)
        # (true value -2). All three gpt-4o solves hit the 400-token cap in the
        # middle of LaTeX synthetic division with no FINAL ANSWER line, the row
        # was "skipped", and 150 respondents were graded against the wrong key.
        qs = [
            _problem(body="Evaluate the limit of the expression as x approaches -1."),
            _step(
                answer="0",
                answer_type="numeric",
                **{"Body Text": "Evaluate /lim{x,-1,(x**3+2*x**2-x-2)/(x+1)}"},
            ),
            _problem(name="p2", body="Evaluate the limit of the expression as x approaches 2."),
            _step(name="p2", answer="4", **{"Body Text": "Evaluate /lim{x,2,(3*x**2-5*x+2)}"}),
        ]
        T = FakeSolver.TRUNCATED
        # Row 1: every solve and every retry is cut off. Row 2: a normal verify.
        out, report, solver = _run(qs, {1: [T, T, T, T, T, T], 3: ["4", "4", "4"]})
        step = out[1]
        self.assertEqual(step["answerKeyCheck"], "unverified")
        self.assertEqual(step["answerKeyCheckReason"], "insufficient_solves")
        self.assertEqual(step["answerKeySolves"], [])
        self.assertEqual(step["Answer"], "0")  # key untouched, flagged for a human
        self.assertNotIn("answerKeyOriginal", step)
        self.assertEqual(report["unverified"], 1)
        self.assertEqual(report["verified"], 1)
        self.assertEqual(report["skipped"], 0)
        # Each truncated solve was retried exactly once, in plain text.
        row1_calls = [c for c in solver.calls if c["item_id"] == "keycheck_1"]
        self.assertEqual(len(row1_calls), 6)
        retries = [c for c in row1_calls if akc.TRUNCATION_RETRY_INSTRUCTION in c["prompt"]]
        self.assertEqual(len(retries), 3)
        self.assertEqual(sorted(c["seed"] for c in retries), sorted(set(c["seed"] for c in row1_calls)))
        for c in row1_calls:
            self.assertEqual(c["max_tokens"], 1200)

        # Never scored: the row builder holds it out and the filter keeps it visible.
        items_df, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(out)
        self.assertIsNone(index_to_item_id[1])
        self.assertEqual(skip_reasons, {1: "answer_key_unverified"})
        self.assertEqual(list(items_df["answer_key"]), ["4"])
        item_id = index_to_item_id[3]
        filtered, debug, _ = filter_questions_by_difficulty_range(
            out,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_CALIBRATED,
            params_lookup={item_id: {"difficulty_logit": 1.0}},
            include_unscored=True,
        )
        by_index = {r["index"]: r for r in debug}
        self.assertTrue(by_index[1]["kept"])
        self.assertFalse(by_index[1]["scored"])
        self.assertIsNone(by_index[1]["difficultyLogit"])
        self.assertEqual(by_index[1]["reason"], "answer_key_unverified")
        held = next(q for q in filtered if q["Row Type"] == "step" and q["Answer"] == "0")
        self.assertFalse(held["difficultyScored"])
        self.assertIsNone(held["difficultyLogit"])
        self.assertEqual(held["difficultyStatus"], "answer_key_unverified")
        self.assertEqual(held["answerKeyCheckReason"], "insufficient_solves")
        # Not held out when include_unscored is off either: the row is dropped, not scored.
        filtered_off, _, _ = filter_questions_by_difficulty_range(
            out,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_CALIBRATED,
            params_lookup={item_id: {"difficulty_logit": 1.0}},
            include_unscored=False,
        )
        self.assertEqual([q["Answer"] for q in filtered_off if q["Row Type"] == "step"], ["4"])

    def test_truncated_solve_retry_that_finishes_counts_as_a_solve(self):
        qs = [_problem(), _step(answer="0")]
        T = FakeSolver.TRUNCATED
        # Seed A: truncated then -2 on retry. Seed B: -2 first time. Seed C: truncated twice.
        out, report, solver = _run(qs, {1: [T, "-2", "-2", T, T]})
        self.assertEqual(out[1]["answerKeyCheck"], "corrected")
        self.assertEqual(out[1]["Answer"], "-2")
        self.assertEqual(out[1]["answerKeyOriginal"], "0")
        self.assertEqual(out[1]["answerKeySolves"], ["-2", "-2"])
        self.assertEqual(report["corrected"], 1)
        self.assertEqual(len(solver.calls), 5)
        retries = [c for c in solver.calls if akc.TRUNCATION_RETRY_INSTRUCTION in c["prompt"]]
        self.assertEqual(len(retries), 2)
        for c in retries:
            self.assertTrue(c["prompt"].startswith("Solve the following college algebra item."))
            self.assertIn("no LaTeX", c["prompt"])
            self.assertIn("at most five short lines", c["prompt"])

    def test_mc_and_image_rows_are_skipped_without_calls(self):
        qs = [
            _problem(),
            _step(answer="A", answer_type="mc", mcChoices="A|B|C"),
            _problem(name="p2"),
            _step(name="p2", answer="7", Images="https://example.org/fig.png"),
            _problem(name="p3", body="See the graph."),
        ]
        qs[4]["Images"] = "https://example.org/graph.png"
        qs.append(_step(name="p3", answer="3"))
        out, report, solver = _run(qs, {})
        self.assertEqual(out[1]["answerKeyCheck"], "skipped")
        self.assertEqual(out[1]["answerKeyCheckReason"], "mc")
        self.assertEqual(out[3]["answerKeyCheck"], "skipped")
        self.assertEqual(out[3]["answerKeyCheckReason"], "has_image")
        self.assertEqual(out[5]["answerKeyCheck"], "skipped")
        self.assertEqual(out[5]["answerKeyCheckReason"], "has_image")
        self.assertEqual(solver.calls, [])
        self.assertEqual(report["skipped"], 3)
        self.assertEqual(report["checked"], 3)
        for i in (0, 2, 4):
            self.assertNotIn("answerKeyCheck", out[i])

    def test_skips_keys_that_are_not_one_gradable_answer(self):
        labelled = "Degree: 4, leading: 5x**4, leading coefficient: 5"
        qs = [
            _problem(),
            _step(answer=labelled, answer_type="algebra"),
            _step(answer="Degree 4 and leading term 5x^4", answer_type="algebra"),
            _step(answer="a=b=c", answer_type="algebra"),
            _step(answer="about five", answer_type="numeric"),
            _step(answer="81x^2-16", answer_type="algebra"),  # ^ is gradable now
            _step(answer="4𝑥^3+20𝑥^2+4𝑥−1", answer_type="algebra"),
        ]
        out, report, solver = _run(
            qs, {5: ["81x^2 - 16"] * 3, 6: ["4x^3 + 20x^2 + 4x - 1"] * 3}
        )
        for i in (1, 2, 3, 4):
            self.assertEqual(out[i]["answerKeyCheck"], "skipped", msg=f"row {i}")
            self.assertEqual(out[i]["answerKeyCheckReason"], "key_not_gradable", msg=f"row {i}")
            self.assertEqual(out[i]["Answer"], qs[i]["Answer"], msg=f"row {i}")
            self.assertNotIn("answerKeyOriginal", out[i])
            self.assertNotIn("answerKeySolves", out[i])
        self.assertEqual(out[5]["answerKeyCheck"], "verified")
        self.assertEqual(out[6]["answerKeyCheck"], "verified")
        self.assertEqual(out[6]["Answer"], "4𝑥^3+20𝑥^2+4𝑥−1")  # verified keys are untouched
        self.assertEqual(len(solver.calls), 6)
        self.assertEqual(report["skipped"], 4)
        self.assertEqual(report["verified"], 2)
        self.assertEqual(report["checked"], 6)

    def test_key_is_gradable_rules(self):
        self.assertTrue(akc.key_is_gradable("numeric", "25/3"))
        self.assertTrue(akc.key_is_gradable("numeric", "-3"))
        # numeric must be a plain number: never collapse multi-value keys.
        self.assertFalse(akc.key_is_gradable("numeric", "x = 7"))
        self.assertFalse(akc.key_is_gradable("numeric", "3,1"))
        self.assertFalse(akc.key_is_gradable("numeric", "(5,-1)"))
        self.assertFalse(akc.key_is_gradable("numeric", "about five"))
        self.assertTrue(akc.key_is_gradable("algebra", "x^2-1"))
        self.assertTrue(akc.key_is_gradable("algebra", "y=2x+1"))
        self.assertTrue(akc.key_is_gradable("algebra", "7"))
        self.assertTrue(akc.key_is_gradable("algebra", "3/5*x-3"))
        # multi-answer algebra keys: top-level comma, "or", or a tuple.
        self.assertFalse(akc.key_is_gradable("algebra", "2,3"))
        self.assertFalse(akc.key_is_gradable("algebra", "x=2 or x=3"))
        self.assertFalse(akc.key_is_gradable("algebra", "(5,-1)"))
        self.assertFalse(akc.key_is_gradable("algebra", "(3,5)"))
        self.assertFalse(akc.key_is_gradable("algebra", "Degree: 3, leading: -p^3, leading coefficient: -1"))
        self.assertFalse(akc.key_is_gradable("algebra", "leading coefficient 5"))
        self.assertFalse(akc.key_is_gradable("algebra", ""))

    def test_multi_answer_keys_are_skipped_not_collapsed(self):
        qs = [
            _problem(),
            _step(answer="3,1", answer_type="numeric"),
            _step(answer="(5,-1)", answer_type="numeric"),
            _step(answer="2,3", answer_type="algebra"),
            _step(answer="x=2 or x=3", answer_type="algebra"),
            _step(answer="-4/3", answer_type="algebra"),
        ]
        # Row 5: the solver writes "x = -4/3"; with the fraction fix this verifies.
        out, report, solver = _run(qs, {5: ["x = -4/3"] * 3})
        for i in (1, 2, 3, 4):
            self.assertEqual(out[i]["answerKeyCheck"], "skipped", msg=f"row {i}")
            self.assertEqual(out[i]["answerKeyCheckReason"], "key_not_gradable", msg=f"row {i}")
            self.assertEqual(out[i]["Answer"], qs[i]["Answer"], msg=f"row {i}")
            self.assertNotIn("answerKeyOriginal", out[i])
        self.assertEqual(out[5]["answerKeyCheck"], "verified")
        self.assertEqual(out[5]["Answer"], "-4/3")
        self.assertEqual(len(solver.calls), 3)
        self.assertEqual(report["skipped"], 4)
        self.assertEqual(report["verified"], 1)
        self.assertEqual(report["corrected"], 0)

    def test_problem_rows_and_blank_answers_pass_through(self):
        qs = [_problem(), _step(answer="")]
        out, report, solver = _run(qs, {})
        self.assertEqual(out[0], qs[0])
        self.assertEqual(out[1], qs[1])
        self.assertEqual(solver.calls, [])
        self.assertEqual(report["checked"], 0)

    def test_input_list_is_not_mutated(self):
        qs = [_problem(), _step(answer="11"), _step(name="p1", answer="5")]
        snapshot = copy.deepcopy(qs)
        out, _, _ = _run(qs, {1: ["7", "7", "7"], 2: ["5", "5", "5"]})
        self.assertEqual(qs, snapshot)
        self.assertIsNot(out[1], qs[1])
        self.assertEqual(out[1]["Answer"], "7")
        self.assertEqual(qs[1]["Answer"], "11")

    def test_report_counts_and_fields(self):
        qs = [
            _problem(),
            _step(answer="7"),
            _step(answer="11"),
            _step(answer="4"),
            _step(answer="9"),
            _step(answer="A", answer_type="mc", mcChoices="A|B"),
        ]
        out, report, _ = _run(
            qs,
            {
                1: ["7", "7", "7"],
                2: ["7", "7", "7"],
                3: ["1", "2", "3"],
                4: [RuntimeError("x"), RuntimeError("y"), "9"],
            },
        )
        # Row 4 (two call errors, one solve) is unverified, not skipped: the
        # key was never checked, so it must not be scored against.
        self.assertEqual(
            {k: report[k] for k in ("checked", "verified", "corrected", "unverified", "skipped")},
            {"checked": 5, "verified": 1, "corrected": 1, "unverified": 2, "skipped": 1},
        )
        self.assertEqual(out[4]["answerKeyCheck"], "unverified")
        self.assertEqual(out[4]["answerKeyCheckReason"], "insufficient_solves")
        self.assertEqual(out[3]["answerKeyCheck"], "unverified")
        self.assertNotIn("answerKeyCheckReason", out[3])
        self.assertEqual(report["model"], "fake-model")
        self.assertIsInstance(report["durationMs"], float)
        self.assertGreaterEqual(report["durationMs"], 0.0)

    def test_solver_calls_use_temperature_zero_distinct_seeds_and_token_cap(self):
        qs = [_problem(), _step(answer="7")]
        _, _, solver = _run(qs, {1: ["7", "7", "7"]})
        seeds = [c["seed"] for c in solver.calls]
        self.assertEqual(len(set(seeds)), 3)
        for call in solver.calls:
            self.assertEqual(call["temperature"], 0.0)
            self.assertEqual(call["max_tokens"], 1200)
            self.assertEqual(call["max_tokens"], akc.KEYCHECK_TOKEN_CAP)
            self.assertEqual(call["model_name"], "fake-model")
            self.assertIn("FINAL ANSWER", call["system_prompt"])
            # Problem stem + step stem in the item text.
            self.assertIn("2x^2-3x+1", call["prompt"])
            self.assertIn("definite integral", call["prompt"])

    def test_algebra_corrected_equation_is_not_mangled(self):
        qs = [_problem(body="Write the line through (0,-3) with slope 4."), _step(answer="y=4x+3", answer_type="algebra")]
        out, _, _ = _run(qs, {1: ["y = 4x - 3", "y=4x-3", "y=4*x-3"]})
        self.assertEqual(out[1]["answerKeyCheck"], "corrected")
        self.assertEqual(out[1]["Answer"], "y = 4x - 3")
        self.assertEqual(out[1]["answerKeyOriginal"], "y=4x+3")

    def test_whole_run_failure_marks_rows_skipped_and_keeps_answers(self):
        qs = [_problem(), _step(answer="11")]

        def failing_run(coro):
            coro.close()  # avoid "coroutine was never awaited"
            raise RuntimeError("no loop")

        with mock.patch.object(akc, "_run_async", side_effect=failing_run):
            out, report = akc.verify_answer_keys(qs, model="fake-model")
        self.assertEqual(out[1]["answerKeyCheck"], "skipped")
        self.assertEqual(out[1]["answerKeyCheckReason"], "check_failed")
        self.assertEqual(out[1]["Answer"], "11")
        self.assertEqual(report["skipped"], 1)

    def test_progress_callback_receives_checking_keys_phase(self):
        qs = [_problem(), _step(answer="7")]
        events = []
        _run(qs, {1: ["7", "7", "7"]}, progress_callback=lambda d, t, p: events.append((d, t, p)))
        self.assertEqual(events, [(0, 1, "checking_keys")])

    def test_model_env_override(self):
        with mock.patch.dict(os.environ, {"QUESTION_DIFFICULTY_KEYCHECK_MODEL": "gpt-4.1"}):
            self.assertEqual(akc.keycheck_model(), "gpt-4.1")
        with mock.patch.dict(os.environ, {"QUESTION_DIFFICULTY_KEYCHECK_MODEL": ""}, clear=False):
            os.environ.pop("QUESTION_DIFFICULTY_CALIBRATION_MODEL", None)
            self.assertEqual(akc.keycheck_model(), "gpt-4o")


class ParseFinalAnswerTests(unittest.TestCase):
    def test_parses_last_final_answer_line(self):
        text = "FINAL ANSWER: 3\nActually wait.\nFINAL ANSWER: -1/18"
        self.assertEqual(akc.parse_final_answer(text), "-1/18")

    def test_strips_markdown_and_latex(self):
        self.assertEqual(akc.parse_final_answer("**FINAL ANSWER: $7$**"), "7")
        self.assertEqual(akc.parse_final_answer("final answer:   x = 3 "), "x = 3")

    def test_none_when_missing(self):
        self.assertIsNone(akc.parse_final_answer("The answer is 7."))
        self.assertIsNone(akc.parse_final_answer(""))
        self.assertIsNone(akc.parse_final_answer("FINAL ANSWER:"))


class OatutorQuestionSolutionTests(unittest.TestCase):
    def test_to_json_carries_solution_before_answer(self):
        q = OatutorQuestion(
            problem_name="p1",
            row_type="step",
            title="",
            body_text="Compute 3+4.",
            solution="3+4=7, so the value is 7.",
            answer="7",
            answer_type="numeric",
            mc_choices="",
        )
        data = q.to_json()
        self.assertEqual(data["Solution"], "3+4=7, so the value is 7.")
        self.assertEqual(data["Answer"], "7")
        keys = list(data.keys())
        self.assertLess(keys.index("Solution"), keys.index("Answer"))
        self.assertEqual(keys.index("Answer") - keys.index("Solution"), 1)

    def test_solution_defaults_to_blank(self):
        q = OatutorQuestion(
            problem_name="p1",
            row_type="problem",
            body_text="Stem",
            answer="",
            answer_type="numeric",
            mc_choices="",
        )
        self.assertEqual(q.solution, "")
        self.assertEqual(q.to_json()["Solution"], "")

    def test_field_order_puts_solution_immediately_before_answer(self):
        fields = list(OatutorQuestion.model_fields.keys())
        self.assertEqual(fields.index("answer") - fields.index("solution"), 1)
        self.assertEqual(fields[fields.index("solution") - 1], "body_text")

    def test_body_text_description_says_working_goes_in_solution(self):
        desc = OatutorQuestion.model_fields["body_text"].description or ""
        self.assertIn("solution", desc.lower())
        self.assertIn("never", desc.lower())


class RowBuilderUnverifiedTests(unittest.TestCase):
    def test_row_builder_skips_unverified_rows(self):
        questions = [
            _problem(),
            _step(answer="11", answerKeyCheck="unverified", answerKeySolves=["7", "8", "9"]),
            _step(name="p1", answer="7", answerKeyCheck="verified"),
            _step(name="p1", answer="4", answerKeyCheck="corrected", answerKeyOriginal="5"),
            _step(name="p1", answer="3", answerKeyCheck="skipped"),
        ]
        items_df, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(questions)
        self.assertEqual(len(items_df), 3)
        self.assertIsNone(index_to_item_id[1])
        self.assertEqual(skip_reasons, {1: "answer_key_unverified"})
        for i in (2, 3, 4):
            self.assertIsNotNone(index_to_item_id[i])
        # Corrected key is what the respondents are graded against.
        self.assertIn("4", set(items_df["answer_key"]))

        _filtered, debug, _meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_INSUFFICIENT,
            include_unscored=True,
        )
        unverified = next(r for r in debug if r["index"] == 1)
        self.assertEqual(unverified["reason"], "answer_key_unverified")
        self.assertIsNone(unverified["itemId"])
        # Kept as an unscored row (include_unscored=True) so the user can fix it.
        self.assertTrue(unverified["kept"])
        self.assertFalse(unverified["scored"])

    def test_row_builder_skips_answer_in_text_and_keeps_row_visible(self):
        questions = [
            _problem(),
            _step(
                answer="y=-2/3x+5/3",
                answerKeyCheck="skipped",
                answerKeyCheckReason="answer_in_text",
            ),
            _step(name="p1", answer="7", answerKeyCheck="verified"),
        ]
        items_df, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(questions)
        self.assertEqual(len(items_df), 1)
        self.assertIsNone(index_to_item_id[1])
        self.assertEqual(skip_reasons[1], "answer_in_text")
        self.assertIsNotNone(index_to_item_id[2])
        _filtered, debug, _meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_INSUFFICIENT,
            include_unscored=True,
        )
        leaked = next(r for r in debug if r["index"] == 1)
        self.assertEqual(leaked["reason"], "answer_in_text")
        self.assertTrue(leaked["kept"])
        self.assertFalse(leaked["scored"])

    def test_row_builder_skips_key_not_gradable_and_keeps_row_visible(self):
        questions = [
            _problem(),
            _step(
                answer="x=5 or x=-1",
                answer_type="algebra",
                answerKeyCheck="skipped",
                answerKeyCheckReason="key_not_gradable",
            ),
            _step(
                name="p1",
                answer="x=4, y=2",
                answer_type="algebra",
                answerKeyCheck="skipped",
                answerKeyCheckReason="key_not_gradable",
            ),
            _step(name="p1", answer="7", answerKeyCheck="verified"),
        ]
        items_df, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(questions)
        self.assertEqual(len(items_df), 1)
        self.assertIsNone(index_to_item_id[1])
        self.assertIsNone(index_to_item_id[2])
        self.assertEqual(skip_reasons[1], "key_not_gradable")
        self.assertEqual(skip_reasons[2], "key_not_gradable")
        self.assertIsNotNone(index_to_item_id[3])
        _filtered, debug, _meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_INSUFFICIENT,
            include_unscored=True,
        )
        skipped = [r for r in debug if r["index"] in (1, 2)]
        self.assertEqual(len(skipped), 2)
        for row in skipped:
            self.assertEqual(row["reason"], "key_not_gradable")
            self.assertTrue(row["kept"])
            self.assertFalse(row["scored"])

    def _unverified_fixture(self):
        # Problem A has only an unverified step; problem B has a verified step.
        questions = [
            _problem(name="A"),
            _step(name="A", answer="11", answerKeyCheck="unverified", answerKeySolves=["7", "8", "9"]),
            _problem(name="B"),
            _step(name="B", answer="7", answerKeyCheck="verified"),
        ]
        _, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(questions)
        self.assertIsNone(index_to_item_id[1])
        self.assertIsNotNone(index_to_item_id[3])
        return questions, index_to_item_id, skip_reasons

    def _filter(self, include_unscored):
        questions, index_to_item_id, skip_reasons = self._unverified_fixture()
        item_id = index_to_item_id[3]
        filtered, debug, meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_CALIBRATED,
            params_lookup={item_id: {"difficulty_logit": 0.2}},
            include_unscored=include_unscored,
        )
        return filtered, debug, meta

    def test_unverified_step_and_its_problem_row_kept_when_include_unscored(self):
        filtered, debug, meta = self._filter(include_unscored=True)
        by_index = {r["index"]: r for r in debug}
        self.assertTrue(by_index[1]["kept"])
        self.assertFalse(by_index[1]["scored"])
        self.assertEqual(by_index[1]["reason"], "answer_key_unverified")
        self.assertIsNone(by_index[1]["itemId"])
        self.assertTrue(by_index[0]["kept"])
        self.assertEqual(by_index[0]["reason"], "companion_problem_row")

        names = [(q["Problem Name"], q["Row Type"]) for q in filtered]
        self.assertEqual(names, [("A", "problem"), ("A", "step"), ("B", "problem"), ("B", "step")])
        step_a = filtered[1]
        self.assertEqual(step_a["difficultyStatus"], "answer_key_unverified")
        self.assertFalse(step_a["difficultyScored"])
        self.assertIsNone(step_a["difficultyLogit"])
        self.assertEqual(step_a["answerKeyCheck"], "unverified")
        self.assertEqual(step_a["Answer"], "11")
        self.assertEqual(meta["keptUnscored"], 1)
        self.assertEqual(meta["totalAfter"], 4)

    def test_unverified_step_and_its_problem_row_dropped_when_not_include_unscored(self):
        filtered, debug, meta = self._filter(include_unscored=False)
        by_index = {r["index"]: r for r in debug}
        self.assertFalse(by_index[1]["kept"])
        self.assertEqual(by_index[1]["reason"], "answer_key_unverified")
        self.assertFalse(by_index[0]["kept"])
        names = [(q["Problem Name"], q["Row Type"]) for q in filtered]
        self.assertEqual(names, [("B", "problem"), ("B", "step")])
        self.assertEqual(meta["keptUnscored"], 0)

    def test_intermediate_step_key_rows_keep_old_behavior(self):
        questions = [
            _problem(name="A"),
            _step(name="A", answer="3x=15", answer_type="numeric"),
        ]
        _, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(questions)
        self.assertEqual(skip_reasons, {1: "intermediate_step_key"})
        filtered, debug, _ = filter_questions_by_difficulty_range(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_CALIBRATED,
            params_lookup={},
            include_unscored=True,
        )
        by_index = {r["index"]: r for r in debug}
        self.assertFalse(by_index[1]["kept"])
        self.assertEqual(by_index[1]["reason"], "intermediate_step_key")
        self.assertFalse(by_index[0]["kept"])
        self.assertEqual(filtered, [])


class EventLoggerAnswerKeyFieldsTests(unittest.TestCase):
    def _capture(self, **kwargs):
        captured = {}

        def fake_log_event(collection, data):
            captured["collection"] = collection
            captured["data"] = data
            return None

        with mock.patch.object(event_logger, "log_event", side_effect=fake_log_event):
            event_logger.log_difficulty_estimation(
                source="after_generation",
                n_respondents=50,
                context={},
                duration_ms=12.0,
                **kwargs,
            )
        return captured["data"]

    def test_item_results_carry_answer_key_check_reason(self):
        questions = [
            _problem(),
            _step(answer="3,1", answerKeyCheck="skipped", answerKeyCheckReason="key_not_gradable"),
            _step(name="p1", answer="A", answer_type="mc", answerKeyCheck="skipped", answerKeyCheckReason="mc"),
            _step(name="p1", answer="5", answerKeyCheck="verified", answerKeySolves=["5", "5", "5"]),
        ]
        scores = [
            {"index": i, "itemId": f"i{i}", "problemName": "p1", "difficultyLogit": None, "scored": False}
            for i in (1, 2, 3)
        ]
        data = self._capture(
            questions=questions,
            scores=scores,
            calibration_meta={
                "answerKeyCheck": {"checked": 3, "verified": 1, "corrected": 0, "unverified": 0, "skipped": 2, "model": "m", "durationMs": 1.0},
            },
        )
        items = data["itemResults"]
        self.assertEqual(items[0]["answerKeyCheck"], "skipped")
        self.assertEqual(items[0]["answerKeyCheckReason"], "key_not_gradable")
        self.assertEqual(items[1]["answerKeyCheckReason"], "mc")
        self.assertEqual(items[2]["answerKeyCheck"], "verified")
        self.assertNotIn("answerKeyCheckReason", items[2])  # None values are not written
        # Same via the meta fallback used when the logged rows predate the check.
        data = self._capture(
            questions=[_problem(), _step(answer="3,1")],
            scores=[{"index": 1, "itemId": "i1", "problemName": "p1", "difficultyLogit": None, "scored": False}],
            calibration_meta={
                "answerKeyCheck": {"checked": 1, "verified": 0, "corrected": 0, "unverified": 0, "skipped": 1, "model": "m", "durationMs": 1.0},
                "answerKeyCheckItems": [{"index": 1, "answerKeyCheck": "skipped", "answerKeyCheckReason": "key_not_gradable"}],
            },
        )
        self.assertEqual(data["itemResults"][0]["answerKeyCheckReason"], "key_not_gradable")

    def test_item_results_carry_key_check_fields_from_rows(self):
        questions = [
            _problem(),
            _step(answer="7", answerKeyCheck="corrected", answerKeyOriginal="11", answerKeySolves=["7", "7", "7"]),
            _step(name="p1", answer="4", answerKeyCheck="verified", answerKeySolves=["4", "4", "4"]),
            _step(name="p1", answer="9"),
        ]
        scores = [
            {"index": 1, "itemId": "i1", "problemName": "p1", "difficultyLogit": 0.1, "scored": True},
            {"index": 2, "itemId": "i2", "problemName": "p1", "difficultyLogit": 0.2, "scored": True},
            {"index": 3, "itemId": "i3", "problemName": "p1", "difficultyLogit": None, "scored": False, "reason": "x"},
        ]
        data = self._capture(
            questions=questions,
            scores=scores,
            calibration_meta={
                "calibrationStatus": "ok",
                "answerKeyCheck": {
                    "checked": 2, "verified": 1, "corrected": 1, "unverified": 0,
                    "skipped": 0, "model": "gpt-4o", "durationMs": 1234.5,
                },
            },
        )
        items = data["itemResults"]
        self.assertEqual(items[0]["answerKeyCheck"], "corrected")
        self.assertEqual(items[0]["answerKeyOriginal"], "11")
        self.assertEqual(items[0]["answerKeySolves"], ["7", "7", "7"])
        self.assertEqual(items[0]["answer"], "7")
        self.assertEqual(items[1]["answerKeyCheck"], "verified")
        self.assertNotIn("answerKeyOriginal", items[1])
        self.assertNotIn("answerKeyCheck", items[2])
        self.assertEqual(data["answerKeyCheck"]["corrected"], 1)
        self.assertEqual(data["answerKeyCheck"]["model"], "gpt-4o")
        # Existing fields still present.
        for field in ("source", "nRespondents", "model", "temperature", "tokenCap", "itemsScored", "calibrationStatus"):
            self.assertIn(field, data)

    def test_item_results_fall_back_to_meta_items_when_rows_predate_check(self):
        questions = [_problem(), _step(answer="11")]  # request rows, pre-check
        scores = [{"index": 1, "itemId": "i1", "problemName": "p1", "difficultyLogit": 0.1, "scored": True}]
        data = self._capture(
            questions=questions,
            scores=scores,
            calibration_meta={
                "answerKeyCheck": {"checked": 1, "verified": 0, "corrected": 1, "unverified": 0, "skipped": 0, "model": "m", "durationMs": 1.0},
                "answerKeyCheckItems": [
                    {"index": 1, "answerKeyCheck": "corrected", "answerKeyOriginal": "11", "answerKeySolves": ["7", "7", "7"], "Answer": "7"},
                ],
            },
        )
        item = data["itemResults"][0]
        self.assertEqual(item["answerKeyCheck"], "corrected")
        self.assertEqual(item["answerKeyOriginal"], "11")
        self.assertEqual(item["answerKeySolves"], ["7", "7", "7"])
        self.assertEqual(item["answer"], "7")

    def test_no_key_check_fields_when_check_did_not_run(self):
        data = self._capture(
            questions=[_problem(), _step(answer="11")],
            scores=[{"index": 1, "itemId": "i1", "difficultyLogit": 0.1, "scored": True}],
            calibration_meta={"calibrationStatus": "ok"},
        )
        self.assertNotIn("answerKeyCheck", data)
        self.assertNotIn("answerKeyCheck", data["itemResults"][0])


class ChatgptServiceHookTests(unittest.TestCase):
    def _run_hook(self, questions, env):
        def fake_calibrate(**kwargs):
            qs = kwargs["questions"]
            return list(qs), [{"index": i} for i in range(len(qs))], {"calibrationStatus": "ok"}

        def fake_verify(qs, progress_callback=None):
            if progress_callback:
                progress_callback(0, 1, "checking_keys")
            out = [dict(q) for q in qs]
            out[1]["Answer"] = "7"
            out[1]["answerKeyOriginal"] = "11"
            out[1]["answerKeyCheck"] = "corrected"
            out[1]["answerKeySolves"] = ["7", "7", "7"]
            return out, {"checked": 1, "verified": 0, "corrected": 1, "unverified": 0, "skipped": 0, "model": "m", "durationMs": 2.0}

        events = []
        with mock.patch.dict(os.environ, env), mock.patch.object(
            chatgpt_service, "USE_PRECALIBRATED_DIFFICULTY_CSV", False
        ), mock.patch.object(
            chatgpt_service, "auto_calibrate_and_filter_questions", side_effect=fake_calibrate
        ), mock.patch.object(akc, "verify_answer_keys", side_effect=fake_verify) as verify:
            filtered, meta = chatgpt_service.apply_difficulty_filter_to_questions(
                questions, {"noFilter": True}, progress_callback=lambda d, t, p: events.append(p)
            )
        return filtered, meta, verify, events

    def test_hook_runs_check_and_exposes_report(self):
        questions = [_problem(), _step(answer="11")]
        filtered, meta, verify, events = self._run_hook(questions, {"QUESTION_DIFFICULTY_VERIFY_KEYS": "1"})
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(events, ["checking_keys"])
        self.assertEqual(meta["answerKeyCheck"]["corrected"], 1)
        self.assertEqual(
            meta["answerKeyCheckItems"],
            [{"index": 1, "answerKeyCheck": "corrected", "answerKeyOriginal": "11", "answerKeySolves": ["7", "7", "7"], "Answer": "7"}],
        )
        # Returned rows carry the corrected key for the sheet.
        self.assertEqual(filtered[1]["Answer"], "7")
        self.assertEqual(filtered[1]["answerKeyOriginal"], "11")
        self.assertEqual(questions[1]["Answer"], "11")

    def test_answer_key_check_items_carry_rendered_columns_for_corrected_rows_only(self):
        corrected = _step(answer="7")
        corrected.update({
            "Answer Rendered": "$$7$$",
            "Answer Tutoring": "$$7$$",
            "answerKeyCheck": "corrected",
            "answerKeyOriginal": "25/3",
            "answerKeySolves": ["7", "7", "7"],
        })
        verified = _step(answer="3")
        verified.update({
            "Answer Rendered": "$$3$$",
            "Answer Tutoring": "$$3$$",
            "answerKeyCheck": "verified",
            "answerKeySolves": ["3", "3", "3"],
        })
        bare_corrected = _step(answer="7")  # generated without rendered columns
        bare_corrected.update({"answerKeyCheck": "corrected", "answerKeyOriginal": "11"})

        items = chatgpt_service._answer_key_check_items([_problem(), corrected, verified, bare_corrected])

        self.assertEqual(
            items[0],
            {
                "index": 1, "answerKeyCheck": "corrected", "answerKeyOriginal": "25/3",
                "answerKeySolves": ["7", "7", "7"], "Answer": "7",
                "Answer Rendered": "$$7$$", "Answer Tutoring": "$$7$$",
            },
        )
        self.assertEqual(
            items[1],
            {"index": 2, "answerKeyCheck": "verified", "answerKeySolves": ["3", "3", "3"]},
        )
        self.assertNotIn("Answer Rendered", items[1])
        self.assertNotIn("Answer Tutoring", items[1])
        # Corrected row without rendered columns does not gain them.
        self.assertEqual(
            items[2],
            {"index": 3, "answerKeyCheck": "corrected", "answerKeyOriginal": "11", "Answer": "7"},
        )

    def test_hook_disabled_by_env(self):
        questions = [_problem(), _step(answer="11")]
        filtered, meta, verify, events = self._run_hook(questions, {"QUESTION_DIFFICULTY_VERIFY_KEYS": "0"})
        self.assertEqual(verify.call_count, 0)
        self.assertEqual(events, [])
        self.assertNotIn("answerKeyCheck", meta)
        self.assertEqual(filtered[1]["Answer"], "11")

    def test_hook_failure_does_not_block_calibration(self):
        questions = [_problem(), _step(answer="11")]

        def fake_calibrate(**kwargs):
            return list(kwargs["questions"]), [], {"calibrationStatus": "ok"}

        with mock.patch.dict(os.environ, {"QUESTION_DIFFICULTY_VERIFY_KEYS": "1"}), mock.patch.object(
            chatgpt_service, "USE_PRECALIBRATED_DIFFICULTY_CSV", False
        ), mock.patch.object(
            chatgpt_service, "auto_calibrate_and_filter_questions", side_effect=fake_calibrate
        ), mock.patch.object(akc, "verify_answer_keys", side_effect=RuntimeError("boom")):
            filtered, meta = chatgpt_service.apply_difficulty_filter_to_questions(questions, {"noFilter": True})
        self.assertEqual(filtered[1]["Answer"], "11")
        self.assertEqual(meta["answerKeyCheck"], {"error": "boom"})


class TruncationWarningTests(unittest.TestCase):
    """``_maybe_log_truncation`` must use the per-call cap when one is given."""

    @staticmethod
    def _response(completion_tokens, finish_reason="stop"):
        choice = mock.Mock()
        choice.finish_reason = finish_reason
        usage = mock.Mock()
        usage.completion_tokens = completion_tokens
        response = mock.Mock()
        response.choices = [choice]
        response.usage = usage
        return response

    def _warnings(self, response, **kwargs):
        with mock.patch.object(llm_providers, "_LLM_MAX_TOKENS", 150), mock.patch.object(
            llm_providers._logger, "warning"
        ) as warn:
            llm_providers._maybe_log_truncation(
                response, respondent_id="keycheck", item_id="keycheck_1", **kwargs
            )
        return warn

    def test_no_warning_for_300_tokens_under_a_400_cap(self):
        warn = self._warnings(self._response(300), max_tokens=400)
        warn.assert_not_called()

    def test_warns_when_the_per_call_cap_is_hit(self):
        warn = self._warnings(self._response(400), max_tokens=400)
        self.assertEqual(warn.call_count, 1)
        self.assertEqual(warn.call_args.args[1], 400)  # logged cap is the per-call one

    def test_default_cap_still_applies_without_max_tokens(self):
        warn = self._warnings(self._response(163))
        self.assertEqual(warn.call_count, 1)
        self.assertEqual(warn.call_args.args[1], 150)
        self._warnings(self._response(120)).assert_not_called()

    def test_finish_reason_length_always_warns(self):
        warn = self._warnings(self._response(50, finish_reason="length"), max_tokens=400)
        self.assertEqual(warn.call_count, 1)


if __name__ == "__main__":
    unittest.main()
