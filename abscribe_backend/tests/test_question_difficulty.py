"""Tests for anchor FIPC question difficulty calibration."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from abscribe_backend.services.question_difficulty.anchors import (
    anchor_calibration_enabled,
    load_anchor_bundle,
)
from abscribe_backend.services.question_difficulty.difficulty_service import (
    CALIBRATION_CALIBRATED,
    CALIBRATION_INSUFFICIENT,
    DEFAULT_EXISTING_SCORE_TEMPERATURE,
    FREE_RASCH_INSUFFICIENT_MESSAGE,
    SATURATION_FALLBACK_NO_RASCH,
    SATURATION_LOW_CONFIDENCE,
    _existing_score_temperature,
    _generation_namespace,
    _is_rasch_dof_error,
    _oatutor_questions_to_items_df,
    _oatutor_row_stem_text,
    _split_informative_new_items,
    auto_calibrate_and_filter_questions,
    filter_questions_by_difficulty_range,
    load_item_params_lookup,
    resolve_difficulty_filter,
    difficulty_meta_for_api,
    sanitize_for_json,
    run_rasch_fit,
    score_existing_questions,
)
from abscribe_backend.services.question_difficulty.autograder import (
    algebra_key_parses,
    grade,
    is_plain_numeric_key,
    normalize_numeric,
)
from abscribe_backend.services.question_difficulty.respondents import (
    build_respondent_personas,
    respondent_id_order,
)

SERVICES_DIR = (
    Path(__file__).resolve().parents[1]
    / "abscribe_backend"
    / "services"
    / "question_difficulty"
)
ANCHORS_DIR = SERVICES_DIR / "anchors"


def _sample_questions(problem_name: str = "test_problem") -> list[dict]:
    return [
        {
            "Problem Name": problem_name,
            "Row Type": "problem",
            "Body Text": "Solve for x in the equation 2x+1=5.",
            "Answer": "",
            "answerType": "",
            "mcChoices": "",
        },
        {
            "Problem Name": problem_name,
            "Row Type": "step",
            "Body Text": "What is x?",
            "Answer": "2",
            "answerType": "numeric",
            "mcChoices": "",
        },
    ]


def _two_step_questions(problem_name: str = "sat_mix") -> list[dict]:
    return [
        {
            "Problem Name": problem_name,
            "Row Type": "problem",
            "Body Text": "Mixed saturation test.",
            "Answer": "",
            "answerType": "",
            "mcChoices": "",
        },
        {
            "Problem Name": problem_name,
            "Row Type": "step",
            "Body Text": "Informative step",
            "Answer": "2",
            "answerType": "numeric",
            "mcChoices": "",
        },
        {
            "Problem Name": problem_name,
            "Row Type": "step",
            "Body Text": "Saturated step",
            "Answer": "99",
            "answerType": "numeric",
            "mcChoices": "",
        },
    ]


def _five_step_questions(problem_name: str = "all_sat") -> list[dict]:
    rows: list[dict] = [
        {
            "Problem Name": problem_name,
            "Row Type": "problem",
            "Body Text": "All-saturate batch test.",
            "Answer": "",
            "answerType": "",
            "mcChoices": "",
        }
    ]
    for i in range(1, 6):
        rows.append(
            {
                "Problem Name": problem_name,
                "Row Type": "step",
                "Body Text": f"Step {i}",
                "Answer": str(i),
                "answerType": "numeric",
                "mcChoices": "",
            }
        )
    return rows


from abscribe_backend.models.oatutor_hints import OatutorQuestion


class TestOatutorQuestionLatex(unittest.TestCase):
    def test_to_json_includes_rendered_columns(self):
        question = OatutorQuestion(
            problem_name="test_problem",
            row_type="step",
            title="Find abs(x)",
            body_text="What is abs(-3)?",
            answer="3",
            answer_type="numeric",
            mc_choices="",
        )
        rendered = question.to_json()
        self.assertIn("Title Rendered", rendered)
        self.assertIn("Body Text Rendered", rendered)
        self.assertIn("Answer Rendered", rendered)
        self.assertTrue(rendered["Title Rendered"])
        self.assertTrue(rendered["Body Text Rendered"])


class TestRespondentPersonas(unittest.TestCase):
    def test_personas_are_stable_and_ordered(self):
        personas = build_respondent_personas(n_respondents=50, base_seed=42)
        self.assertEqual(len(personas), 50)
        self.assertEqual(personas[0].respondent_id, "R001")
        self.assertEqual(personas[0].seed, 43)
        self.assertIn("R001", personas[0].system_prompt)
        self.assertEqual(
            [p.respondent_id for p in personas], respondent_id_order(50)
        )


class TestItemIdentity(unittest.TestCase):
    def test_step_gets_stable_item_id(self):
        questions = _sample_questions("linear_eq_1")
        items_df, index_map, _, _ = _oatutor_questions_to_items_df(questions)
        self.assertEqual(len(items_df), 1)
        self.assertEqual(items_df.iloc[0]["item_id"], "NEW_linear_eq_1_1")
        self.assertIsNone(index_map[0])
        self.assertEqual(index_map[1], "NEW_linear_eq_1_1")

    def test_filter_joins_on_item_id_not_stem(self):
        questions = _sample_questions()
        _, index_map, _, _ = _oatutor_questions_to_items_df(questions)
        params = {"NEW_test_problem_1": {"item_id": "NEW_test_problem_1", "difficulty_logit": 0.0}}
        filtered, debug, meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-3.0,
            max_logit=3.0,
            params_lookup=params,
            index_to_item_id=index_map,
            calibration_status=CALIBRATION_CALIBRATED,
        )
        self.assertEqual(meta["calibrationStatus"], CALIBRATION_CALIBRATED)
        step_eval = next(r for r in debug if r["rowType"] == "step")
        self.assertIsNotNone(step_eval["difficultyLogit"])
        self.assertNotEqual(step_eval.get("reason"), "no_stem_match")
        self.assertEqual(len(filtered), 2)  # problem + step companion


class TestDifficultyFilterControls(unittest.TestCase):
    def test_resolve_difficulty_filter_from_levels(self):
        resolved = resolve_difficulty_filter(
            {"minLevel": 2, "maxLevel": 4, "maxCount": 3, "includeUnscored": False},
            default_min_logit=-3.0,
            default_max_logit=3.0,
        )
        self.assertEqual(resolved["min_logit"], -1.0)
        self.assertEqual(resolved["max_logit"], 1.0)
        self.assertEqual(resolved["max_count"], 3)
        self.assertFalse(resolved["include_unscored"])
        self.assertEqual(resolved["n_respondents"], 150)

    def test_resolve_difficulty_filter_n_respondents_override(self):
        resolved = resolve_difficulty_filter(
            {"minLevel": 1, "maxLevel": 5, "nRespondents": 10},
            default_min_logit=-3.0,
            default_max_logit=3.0,
        )
        self.assertEqual(resolved["n_respondents"], 10)

    def test_resolve_difficulty_filter_questions_to_generate(self):
        resolved = resolve_difficulty_filter(
            {"minLevel": 1, "maxLevel": 5, "questionsToGenerate": 3},
            default_min_logit=-3.0,
            default_max_logit=3.0,
        )
        self.assertEqual(resolved["questionsToGenerate"], 3)

    def test_resolve_difficulty_filter_no_filter(self):
        resolved = resolve_difficulty_filter(
            {"noFilter": True, "nRespondents": 25},
            default_min_logit=-3.0,
            default_max_logit=3.0,
        )
        self.assertTrue(resolved["noFilter"])
        self.assertEqual(resolved["min_logit"], float("-inf"))
        self.assertEqual(resolved["max_logit"], float("inf"))
        self.assertEqual(resolved["n_respondents"], 25)

    def test_difficulty_meta_for_api_no_filter_is_valid_json(self):
        import json

        meta = {
            "noFilter": True,
            "minLogit": float("-inf"),
            "maxLogit": float("inf"),
            "min_logit": float("-inf"),
            "max_logit": float("inf"),
            "calibrationStatus": "calibrated",
            "totalBefore": 5,
            "totalAfter": 5,
        }
        safe = difficulty_meta_for_api(meta)
        encoded = json.dumps(safe)
        parsed = json.loads(encoded)
        self.assertIsNone(parsed["minLogit"])
        self.assertIsNone(parsed["maxLogit"])
        self.assertIsNone(parsed["min_logit"])
        self.assertIsNone(parsed["max_logit"])
        self.assertTrue(parsed["noFilter"])
        self.assertEqual(parsed["totalAfter"], 5)

    def test_sanitize_for_json_handles_nan_in_nested_evaluations(self):
        import json

        meta = {
            "evaluations": [{"difficultyLogit": 1.0, "seDifficulty": float("nan")}],
        }
        safe = sanitize_for_json(meta)
        self.assertIsNone(safe["evaluations"][0]["seDifficulty"])
        json.dumps(safe)

    def test_filter_count_cap_and_unscored_flag(self):
        questions = [
            {"Problem Name": "p1", "Row Type": "problem", "Body Text": "P1"},
            {
                "Problem Name": "p1",
                "Row Type": "step",
                "Body Text": "S1",
                "Answer": "1",
                "answerType": "numeric",
            },
            {"Problem Name": "p2", "Row Type": "problem", "Body Text": "P2"},
            {
                "Problem Name": "p2",
                "Row Type": "step",
                "Body Text": "S2",
                "Answer": "2",
                "answerType": "numeric",
            },
            {"Problem Name": "p3", "Row Type": "problem", "Body Text": "P3"},
            {
                "Problem Name": "p3",
                "Row Type": "step",
                "Body Text": "S3",
                "Answer": "3",
                "answerType": "numeric",
            },
        ]
        params_lookup = {
            "NEW_p1_1": {"difficulty_logit": -0.5},
            "NEW_p2_2": {"difficulty_logit": 0.0},
            "NEW_p3_3": {"difficulty_logit": 2.0},
        }
        filtered, _debug, _meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-1.0,
            max_logit=1.0,
            params_lookup=params_lookup,
            max_count=1,
            include_unscored=False,
        )
        step_rows = [q for q in filtered if q["Row Type"] == "step"]
        self.assertEqual(len(step_rows), 1)
        self.assertEqual(step_rows[0]["Problem Name"], "p1")

    def test_include_unscored_keeps_null_logit_steps(self):
        questions = _sample_questions("unscored")
        filtered, _debug, _meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-1.0,
            max_logit=1.0,
            params_lookup={},
            calibration_status=CALIBRATION_INSUFFICIENT,
            include_unscored=True,
        )
        self.assertEqual(len(filtered), 2)
        step = next(q for q in filtered if q["Row Type"] == "step")
        self.assertFalse(step["difficultyScored"])
        self.assertEqual(step["difficultyStatus"], CALIBRATION_INSUFFICIENT)


class TestVarianceGuard(unittest.TestCase):
    def test_all_correct_new_item_is_insufficient(self):
        df = pd.DataFrame({"NEW_x_1": [1] * 50}, index=respondent_id_order(50))
        informative, zero_var = _split_informative_new_items(df, ["NEW_x_1"])
        self.assertEqual(informative, [])
        self.assertEqual(zero_var, ["NEW_x_1"])

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_auto_calibration_skips_r_on_zero_variance(
        self, mock_generate: unittest.mock.MagicMock
    ):
        questions = _sample_questions("zero_var")
        run_dir = SERVICES_DIR / "anchors" / "_test_zero_var"
        run_dir.mkdir(exist_ok=True)
        resp = pd.DataFrame(
            {"NEW_zero_var_1": [1] * 50},
            index=respondent_id_order(50),
        )
        resp.reset_index(names="respondent_id").to_csv(
            run_dir / "responses.csv", index=False
        )

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.read_csv(run_dir / "responses.csv").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        filtered, debug, meta = auto_calibrate_and_filter_questions(
            questions, min_logit=-3.0, max_logit=3.0
        )
        self.assertEqual(meta["calibrationStatus"], CALIBRATION_INSUFFICIENT)
        self.assertEqual(len(filtered), len(questions))
        step_eval = next(r for r in debug if r["rowType"] == "step")
        self.assertIsNone(step_eval["difficultyLogit"])
        self.assertEqual(step_eval["reason"], CALIBRATION_INSUFFICIENT)

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_auto_calibration_returns_logit_with_mock_responses(
        self, mock_generate: unittest.mock.MagicMock
    ):
        questions = _sample_questions("cal_ok")
        bundle = load_anchor_bundle()
        n_resp = len(bundle["responses"])
        rng = np.random.default_rng(99)
        new_id = "NEW_cal_ok_1"
        col = [1 if rng.random() < 0.6 else 0 for _ in range(n_resp)]

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {new_id: col},
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "1"},
            clear=False,
        ):
            _filtered, debug, meta = auto_calibrate_and_filter_questions(
                questions, min_logit=-3.0, max_logit=3.0
            )
        self.assertEqual(meta["calibrationStatus"], CALIBRATION_CALIBRATED)
        step_eval = next(r for r in debug if r["rowType"] == "step")
        self.assertIsNotNone(step_eval["difficultyLogit"])
        self.assertNotIn("error", meta)


class TestSaturationRecovery(unittest.TestCase):
    """Feature 2: saturated existing items scored via anchor FIPC (flag-gated)."""

    def setUp(self):
        self._fipc_env = patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "1"},
            clear=False,
        )
        self._fipc_env.start()

    def tearDown(self):
        self._fipc_env.stop()

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_all_correct_saturated_recovery_easy_end(
        self, mock_generate: unittest.mock.MagicMock
    ):
        questions = _sample_questions("sat_easy")
        n_resp = len(load_anchor_bundle()["responses"])

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {"NEW_sat_easy_1": [1] * n_resp},
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        with patch.dict("os.environ", {"QUESTION_DIFFICULTY_KEEP_TEMP_DIR": "1"}):
            _filtered, debug, meta = auto_calibrate_and_filter_questions(
                questions,
                min_logit=-4.0,
                max_logit=4.0,
                score_saturated_via_anchors=True,
            )
        self.assertNotEqual(meta["calibrationStatus"], CALIBRATION_INSUFFICIENT)
        self.assertTrue(meta.get("saturationRecovery"))
        step_eval = next(r for r in debug if r["rowType"] == "step")
        lookup = load_item_params_lookup(
            Path(meta["tempDir"]) / "run" / "item_params.csv"
        )
        rasch_b = lookup["NEW_sat_easy_1"]["difficulty_logit"]
        self.assertTrue(step_eval.get("scored"))
        self.assertIsNotNone(step_eval["difficultyLogit"])
        self.assertEqual(step_eval["reason"], SATURATION_LOW_CONFIDENCE)
        self.assertEqual(step_eval["saturationDirection"], "all_correct")
        self.assertTrue(step_eval.get("lowConfidence"))
        self.assertTrue(step_eval.get("difficultyUncertain"))
        self.assertIsNotNone(step_eval.get("seDifficulty"))
        self.assertAlmostEqual(step_eval["difficultyLogit"], rasch_b, places=4)
        self.assertNotAlmostEqual(step_eval["difficultyLogit"], -2.5, places=2)
        self.assertEqual(step_eval["difficultyLabel"], "Very Easy")

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_all_wrong_saturated_recovery_hard_end(
        self, mock_generate: unittest.mock.MagicMock
    ):
        questions = _sample_questions("sat_hard")
        n_resp = len(load_anchor_bundle()["responses"])

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {"NEW_sat_hard_1": [0] * n_resp},
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        _filtered, debug, meta = auto_calibrate_and_filter_questions(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            score_saturated_via_anchors=True,
        )
        self.assertNotEqual(meta["calibrationStatus"], CALIBRATION_INSUFFICIENT)
        step_eval = next(r for r in debug if r["rowType"] == "step")
        self.assertTrue(step_eval.get("scored"))
        self.assertEqual(step_eval["reason"], SATURATION_LOW_CONFIDENCE)
        self.assertEqual(step_eval["saturationDirection"], "all_wrong")
        self.assertTrue(step_eval.get("lowConfidence"))
        self.assertTrue(step_eval.get("difficultyUncertain"))
        self.assertIsNotNone(step_eval.get("seDifficulty"))
        # Flip-trick Rasch b is mid-scale, not a confident Very Hard band midpoint.
        self.assertLess(step_eval["difficultyLogit"], 1.0)

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_score_existing_questions_uses_saturation_recovery(
        self, mock_generate: unittest.mock.MagicMock
    ):
        questions = _sample_questions("existing_score")
        n_resp = len(load_anchor_bundle()["responses"])

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {"NEW_existing_score_1": [1] * n_resp},
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        out = score_existing_questions(questions)
        self.assertNotEqual(out["calibrationStatus"], CALIBRATION_INSUFFICIENT)
        self.assertIn("respondentAbility", out)
        self.assertIn("meanTheta", out["respondentAbility"])
        step = next(s for s in out["scores"] if s["rowType"] == "step")
        self.assertTrue(step["scored"])
        self.assertIsNotNone(step["difficultyLogit"])
        self.assertEqual(step["reason"], SATURATION_LOW_CONFIDENCE)
        self.assertLessEqual(step["difficultyLogit"], -0.3)
        self.assertEqual(step["difficultyLabel"], "Very Easy")
        self.assertTrue(step.get("lowConfidence"))
        self.assertIsNotNone(step.get("seDifficulty"))

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_mixed_variance_saturated_and_informative_differ(
        self, mock_generate: unittest.mock.MagicMock
    ):
        """Saturated steps infer easy; informative steps keep Rasch differentiation."""
        questions = [
            {
                "Problem Name": "mixed_var",
                "Row Type": "problem",
                "Body Text": "Solve for x.",
                "Answer": "",
                "answerType": "",
                "mcChoices": "",
            },
            {
                "Problem Name": "mixed_var",
                "Row Type": "step",
                "Body Text": "Easy step",
                "Answer": "1",
                "answerType": "numeric",
                "mcChoices": "",
            },
            {
                "Problem Name": "mixed_var",
                "Row Type": "step",
                "Body Text": "Harder step",
                "Answer": "2",
                "answerType": "numeric",
                "mcChoices": "",
            },
        ]
        n_resp = len(load_anchor_bundle()["responses"])
        rng = np.random.default_rng(42)
        easy_col = [1] * n_resp
        hard_col = [1 if rng.random() < 0.35 else 0 for _ in range(n_resp)]

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {
                    "NEW_mixed_var_1": easy_col,
                    "NEW_mixed_var_2": hard_col,
                },
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        _filtered, debug, meta = auto_calibrate_and_filter_questions(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            score_saturated_via_anchors=True,
        )
        self.assertEqual(meta["calibrationStatus"], CALIBRATION_CALIBRATED)
        steps = [r for r in debug if r["rowType"] == "step"]
        self.assertEqual(len(steps), 2)
        easy_step = next(r for r in steps if r["itemId"] == "NEW_mixed_var_1")
        hard_step = next(r for r in steps if r["itemId"] == "NEW_mixed_var_2")
        self.assertEqual(easy_step["reason"], SATURATION_LOW_CONFIDENCE)
        self.assertEqual(easy_step["saturationDirection"], "all_correct")
        self.assertNotEqual(hard_step["reason"], SATURATION_LOW_CONFIDENCE)
        self.assertIsNotNone(hard_step["difficultyLogit"])
        self.assertGreater(hard_step["difficultyLogit"], easy_step["difficultyLogit"])

    def test_saturated_fallback_only_when_no_rasch_in_lookup(self):
        questions = _sample_questions("sat_fallback")
        _, index_to_item_id, _, _ = _oatutor_questions_to_items_df(questions)
        item_id = index_to_item_id[1]
        _filtered, debug, _meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            calibration_status=CALIBRATION_CALIBRATED,
            zero_variance_item_ids={item_id},
            saturation_directions={item_id: "all_wrong"},
            recover_saturated_scores=True,
            include_unscored=True,
        )
        step = next(r for r in debug if r["rowType"] == "step")
        self.assertEqual(step["reason"], SATURATION_FALLBACK_NO_RASCH)
        self.assertAlmostEqual(step["difficultyLogit"], 2.5)
        self.assertEqual(step["saturationDirection"], "all_wrong")
        self.assertTrue(step.get("difficultyUncertain"))


class TestExistingScoreTemperature(unittest.TestCase):
    def test_feature2_default_existing_score_temperature(self):
        self.assertEqual(_existing_score_temperature(), DEFAULT_EXISTING_SCORE_TEMPERATURE)

    def test_generation_namespace_uses_calibration_by_default(self):
        from abscribe_backend.services.question_difficulty.respondents import (
            DEFAULT_CALIBRATION_TEMPERATURE,
        )

        args = _generation_namespace("/tmp/items.csv", 10, "/tmp/out")
        self.assertEqual(args.temperature, DEFAULT_CALIBRATION_TEMPERATURE)

    def test_generation_namespace_accepts_override(self):
        args = _generation_namespace(
            "/tmp/items.csv", 10, "/tmp/out", temperature=0.5
        )
        self.assertEqual(args.temperature, 0.5)


class TestOatutorNotationToPlain(unittest.TestCase):
    def test_frac_vulgar_and_coefficient_parentheses(self):
        from abscribe_backend.services.question_difficulty.difficulty_service import (
            _oatutor_notation_to_plain,
        )

        self.assertEqual(
            _oatutor_notation_to_plain(r"Simplify \frac{3}{4}x + 5"),
            "Simplify (3)/(4)x + 5",
        )
        self.assertEqual(
            _oatutor_notation_to_plain("Evaluate ¾x + ½ − ¼"),
            "Evaluate 3/4x + 1/2 − 1/4",
        )
        self.assertEqual(
            _oatutor_notation_to_plain("Multiply 3/4*x by 8"),
            "Multiply (3/4)*x by 8",
        )


class TestCalibrationStemVisibility(unittest.TestCase):
    def test_evaluation_rows_include_calibration_stem_and_key(self):
        questions = [
            {
                "Problem Name": "frac_demo",
                "Row Type": "problem",
                "Title": "Fractions",
                "Body Text": "",
                "Answer": "",
                "answerType": "",
                "mcChoices": "",
            },
            {
                "Problem Name": "frac_demo",
                "Row Type": "step",
                "Title": r"Simplify \frac{3}{4}x + 5",
                "Body Text": "",
                "Answer": "11",
                "answerType": "algebra",
                "mcChoices": "",
            },
        ]
        params = {
            "NEW_frac_demo_1": {
                "item_id": "NEW_frac_demo_1",
                "difficulty_logit": 0.0,
                "se_difficulty": 0.1,
            }
        }
        _, evaluations, meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-10.0,
            max_logit=10.0,
            params_lookup=params,
        )
        step_row = next(r for r in evaluations if r.get("rowType") == "step")
        self.assertEqual(step_row["calibrationKey"], "11")
        self.assertIn("(3)/(4)x", step_row["calibrationStem"])
        self.assertEqual(len(meta["calibrationItems"]), 1)
        self.assertEqual(meta["calibrationItems"][0]["calibrationKey"], "11")


class TestTitleStemFallback(unittest.TestCase):
    """Calibration stems always merge Title + Body Text."""

    def _real6_questions(self) -> list[dict]:
        return [
            {
                "Problem Name": "real6",
                "Row Type": "problem",
                "Title": "Distributive Property",
                "Body Text": "",
                "Answer": "",
                "answerType": "",
                "mcChoices": "",
            },
            {
                "Problem Name": "real6",
                "Row Type": "step",
                "Title": "What is 4(12+(-7))?",
                "Body Text": "",
                "Answer": "20",
                "answerType": "algebra",
                "mcChoices": "",
            },
        ]

    def test_feature2_uses_title_when_body_text_empty(self):
        items_df, _, _, _ = _oatutor_questions_to_items_df(
            self._real6_questions(), use_title_stem_fallback=True
        )
        self.assertEqual(len(items_df), 1)
        self.assertIn("4(12+(-7))", items_df.iloc[0]["stem"])
        self.assertIn("Distributive Property", items_df.iloc[0]["stem"])

    def test_feature1_default_does_not_use_title(self):
        # Title is included on generate because the model sometimes places the
        # equation in Title while Body Text is generic; a stem without the
        # equation cannot be answered by simulated respondents.
        items_df, _, _, _ = _oatutor_questions_to_items_df(self._real6_questions())
        self.assertEqual(len(items_df), 1)
        self.assertIn("4(12+(-7))", items_df.iloc[0]["stem"])
        self.assertIn("Distributive Property", items_df.iloc[0]["stem"])

    def test_combines_title_and_body_when_both_present(self):
        q = {
            "Title": "Part A",
            "Body Text": "Solve for x.",
        }
        self.assertEqual(
            _oatutor_row_stem_text(q, use_title_stem_fallback=False),
            "Part A Solve for x.",
        )
        self.assertEqual(
            _oatutor_row_stem_text(q, use_title_stem_fallback=True),
            "Part A Solve for x.",
        )


class TestIntermediateStepKeyGuard(unittest.TestCase):
    def test_numeric_intermediate_key_skipped_with_reason(self):
        questions = [
            {
                "Problem Name": "algebra_easy_01",
                "Row Type": "problem",
                "Title": "",
                "Body Text": "",
                "Answer": "",
                "answerType": "",
                "mcChoices": "",
            },
            {
                "Problem Name": "algebra_easy_01",
                "Row Type": "step",
                "Title": "",
                "Body Text": "Solve for x: 3x+5=20. Subtract 5 from both sides.",
                "Answer": "3x=15",
                "answerType": "numeric",
                "mcChoices": "",
            },
        ]
        items_df, index_to_item_id, _, skip_reasons = _oatutor_questions_to_items_df(
            questions
        )
        self.assertTrue(items_df.empty)
        self.assertIsNone(index_to_item_id[1])
        self.assertEqual(skip_reasons.get(1), "intermediate_step_key")

        _filtered, debug, _meta = filter_questions_by_difficulty_range(
            questions,
            min_logit=-4.0,
            max_logit=4.0,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=skip_reasons,
            calibration_status=CALIBRATION_INSUFFICIENT,
            include_unscored=True,
        )
        step = next(r for r in debug if r["rowType"] == "step")
        self.assertEqual(step["reason"], "intermediate_step_key")
        self.assertIsNone(step["itemId"])
        self.assertFalse(step["kept"])


class TestAutograder(unittest.TestCase):
    def test_plain_numeric_key_detection(self):
        self.assertTrue(is_plain_numeric_key("20"))
        self.assertTrue(is_plain_numeric_key("-163"))
        self.assertTrue(is_plain_numeric_key("3/4"))
        self.assertFalse(is_plain_numeric_key("x+2"))
        self.assertFalse(is_plain_numeric_key("2x"))

    def test_algebra_numeric_key_accepts_variants(self):
        key = "20"
        cases = [
            "20",
            "4(12+(-7)) = 20",
            "The answer is 20",
            "4*5",
        ]
        for student in cases:
            with self.subTest(student=student):
                is_correct, rule, _err = grade("algebra", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule}")

    def test_algebra_with_symbolic_key_still_uses_algebra(self):
        is_correct, rule, _err = grade("algebra", "x=2", "x=2")
        self.assertEqual(is_correct, 1)
        self.assertEqual(rule, "algebra_sympy")

    def test_square_root_latex_and_symbol(self):
        cases = [
            ("sqrt(3x-1)", r"\sqrt{3x-1}"),
            (r"3\sqrt{x+5}-4", r"3\sqrt{x+5}-4"),
            ("√(x+9)", "√(x+9)"),
            (r"(\sqrt{x+3})^3-4\sqrt{x+3}", r"(\sqrt{x+3})^3-4\sqrt{x+3}"),
        ]
        for student, key in cases:
            with self.subTest(student=student, key=key):
                is_correct, rule, err = grade("algebra", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={err}")
        self.assertEqual(grade("algebra", "sqrt(3x-1)", r"\sqrt{3x-1}")[0], 1)
        self.assertEqual(grade("algebra", r"3\sqrt{x+5}-4", "3sqrt(x+5)-4")[0], 1)
        self.assertEqual(grade("algebra", "√(x+9)", "sqrt(x+9)")[0], 1)
        self.assertEqual(
            grade("algebra", "(sqrt(x+3))**3-4*sqrt(x+3)", r"(\sqrt{x+3})^3-4\sqrt{x+3}")[0],
            1,
        )

    def test_string_answers_match_case_and_whole_word(self):
        self.assertEqual(grade("string", "perpendicular", "Perpendicular")[0], 1)
        self.assertEqual(grade("string", "Perpendicular.", "perpendicular")[0], 1)
        self.assertEqual(
            grade("string", "the lines are perpendicular", "Perpendicular")[0], 1
        )
        self.assertEqual(grade("string", "parallel", "Perpendicular")[0], 0)
        self.assertEqual(grade("string", "not perpendicularly", "Perpendicular")[0], 0)

    def test_labelled_key_matches_bare_solve(self):
        """Key ``m=2`` against solve ``2`` (the checker's direction)."""
        cases = [
            ("2", "m=2"),
            ("3/2", "m=3/2"),
            ("-9/7", "m1=-9/7"),
            ("2", "x=2"),
        ]
        for student, key in cases:
            with self.subTest(student=student, key=key):
                is_correct, rule, err = grade("algebra", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={err}")
        # Existing inverse path is unchanged.
        self.assertEqual(grade("algebra", "m=2", "2")[0], 1)
        self.assertEqual(grade("algebra", "y=2x+1", "y=2x+1")[0], 1)
        # A real equation whose left side is not a lone identifier is unchanged.
        self.assertEqual(grade("algebra", "2x+1", "5x+3y=24")[0], 0)

    def test_algebra_caret_means_power(self):
        cases = [
            ("x^2-1", "x^2-1"),
            ("25x^2 - 9", "25x**2-9"),
            ("3x^3 + 10x^2 - 23x + 10", "3x**3+10x**2-23x+10"),
            ("y = 2x^2 + 1", "y=2x**2+1"),
        ]
        for student, key in cases:
            with self.subTest(student=student, key=key):
                is_correct, rule, err = grade("algebra", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={err}")
        # Still a real comparison, not a parse accident.
        self.assertEqual(grade("algebra", "x^2+1", "x^2-1")[0], 0)

    def test_algebra_unicode_math_text_maps_to_ascii(self):
        cases = [
            ("4x^3 + 20x^2 + 4x - 1", "4𝑥^3+20𝑥^2+4𝑥−1"),  # math-italic x, Unicode minus
            ("x^2-1", "x²−1"),  # superscript digit
            ("6x", "2×3x"),  # multiplication sign
            ("6x", "2·3x"),  # middle dot
        ]
        for student, key in cases:
            with self.subTest(student=student, key=key):
                is_correct, rule, err = grade("algebra", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={err}")
        self.assertEqual(grade("numeric", "-3", "−3")[0], 1)
        self.assertEqual(normalize_numeric("𝟑"), "3")

    def test_numeric_fraction_after_equals_is_kept_whole(self):
        self.assertEqual(normalize_numeric("x = -4/3"), "-4/3")
        self.assertEqual(normalize_numeric("The answer is 3/4"), "3/4")
        for answer_type, student, key in [
            ("algebra", "x = -4/3", "-4/3"),
            ("algebra", "-4/3", "-4/3"),
            ("numeric", "x = -4/3", "-4/3"),
            ("numeric", "5", "5"),
            ("numeric", "x = 2.5", "2.5"),
        ]:
            with self.subTest(student=student, key=key):
                is_correct, rule, err = grade(answer_type, student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={err}")
        self.assertEqual(grade("algebra", "x = -4", "-4/3")[0], 0)

    def test_numeric_latex_fraction_and_delimiters(self):
        # 2.3 Limit Laws run: ``\(-\frac{7}{3}\)`` was read as ``3`` (the
        # denominator, sign lost). Delimiters go first, then \frac -> a/b.
        self.assertEqual(normalize_numeric(r"$-\frac{7}{3}$"), "-7/3")
        self.assertEqual(normalize_numeric(r"\(-\frac{7}{3}\)"), "-7/3")
        self.assertEqual(normalize_numeric(r"-\(\frac{12}{3}\)"), "-12/3")
        self.assertEqual(normalize_numeric(r"\[\dfrac{9}{4}\]"), "9/4")
        self.assertEqual(normalize_numeric(r"\frac{-9}{3}"), "-9/3")
        self.assertEqual(normalize_numeric(r"The limit is \(-4\)."), "-4")
        self.assertEqual(normalize_numeric("$$56$$"), "56")
        for student, key in [
            (r"$-\frac{7}{3}$", "-7/3"),
            (r"\(-\frac{7}{3}\)", "-7/3"),
            (r"-\(\frac{12}{3}\)", "-4"),
            (r"\dfrac{9}{4}", "2.25"),
            (r"x = -\frac{9}{3}", "-3"),
            (r"The limit is \(-4\).", "-4"),
            (r"\(27\)", "27"),
        ]:
            with self.subTest(student=student, key=key):
                is_correct, rule, err = grade("numeric", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={err}")
        # Still a comparison: -7/3 is not 3 and not -3.
        self.assertEqual(grade("numeric", r"$-\frac{7}{3}$", "3")[0], 0)
        self.assertEqual(grade("numeric", r"$-\frac{7}{3}$", "-3")[0], 0)
        # algebra rows with a plain numeric key go through the same path.
        self.assertEqual(grade("algebra", r"\(-\frac{7}{3}\)", "-7/3")[0], 1)
        self.assertEqual(grade("algebra", r"\(-\frac{7}{3}\)", "-3")[0], 0)

    def test_numeric_non_numeric_answers_wrong_unless_key_matches(self):
        from abscribe_backend.services.question_difficulty.autograder import (
            non_numeric_answer_token,
        )

        self.assertEqual(non_numeric_answer_token("The limit does not exist."), "dne")
        self.assertEqual(non_numeric_answer_token("DNE"), "dne")
        self.assertEqual(non_numeric_answer_token("undefined"), "undefined")
        self.assertEqual(non_numeric_answer_token("infinity"), "infinity")
        self.assertEqual(non_numeric_answer_token("∞"), "infinity")
        self.assertEqual(non_numeric_answer_token(r"\(\infty\)"), "infinity")
        self.assertEqual(non_numeric_answer_token("Limit is ∞ inﬁ."), "infinity")
        self.assertEqual(non_numeric_answer_token(r"= - \(\infty\)"), "-infinity")
        self.assertEqual(non_numeric_answer_token("-∞"), "-infinity")
        self.assertIsNone(non_numeric_answer_token("-3"))
        self.assertIsNone(non_numeric_answer_token("5, not undefined"))
        self.assertIsNone(non_numeric_answer_token(""))

        # Wrong against a numeric key, with a reason that says why.
        for student in ("The limit does not exist.", "DNE", "undefined", "infinity", "∞", "-∞"):
            with self.subTest(student=student):
                is_correct, rule, _err = grade("numeric", student, "-3")
                self.assertEqual(is_correct, 0)
                self.assertEqual(rule, "non_numeric_mismatch")
        # A number against a non-numeric key is wrong too.
        self.assertEqual(grade("numeric", "-3", "DNE")[0], 0)
        # Right only when the key says the same thing.
        self.assertEqual(grade("numeric", "The limit does not exist.", "DNE"), (1, "non_numeric_match", None))
        self.assertEqual(grade("numeric", "does not exist", "does not exist")[0], 1)
        self.assertEqual(grade("numeric", "∞", "infinity")[0], 1)
        self.assertEqual(grade("numeric", r"\infty", "∞")[0], 1)
        self.assertEqual(grade("numeric", "Undefined.", "undefined")[0], 1)
        self.assertEqual(grade("numeric", "-∞", "infinity")[0], 0)
        self.assertEqual(grade("numeric", "DNE", "undefined")[0], 0)
        # algebra rows with a plain numeric key: same verdict.
        self.assertEqual(grade("algebra", "The limit does not exist.", "-3")[0], 0)
        self.assertEqual(grade("algebra", "infinity", "-3")[0], 0)

    def test_algebra_key_parses(self):
        self.assertTrue(algebra_key_parses("x^2-1"))
        self.assertTrue(algebra_key_parses("4𝑥^3+20𝑥^2+4𝑥−1"))
        self.assertTrue(algebra_key_parses("y=2x+1"))
        self.assertTrue(algebra_key_parses("(3x-2)(x^2+4x-5)"))
        self.assertFalse(algebra_key_parses("Degree: 4, leading: 5x**4, leading coefficient: 5"))
        self.assertFalse(algebra_key_parses("a=b=c"))
        self.assertFalse(algebra_key_parses("(5,-1)"))  # a tuple is not one expression
        self.assertFalse(algebra_key_parses(""))

    def test_mc_numeric_equivalence_and_prose(self):
        key = "-163"
        choices = "-149|-163|-313|-326"
        for student in ("-163", "-163.0", "The answer is -163"):
            with self.subTest(student=student):
                is_correct, _rule, _err = grade("mc", student, key, choices)
                self.assertEqual(is_correct, 1)

    def test_algebra_pi_expression(self):
        key = "180π"
        for student in ("180π", "180*pi", "180 pi"):
            with self.subTest(student=student):
                is_correct, rule, _err = grade("algebra", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule}")

    def test_algebra_equation_rhs_matches_symbolic_key(self):
        key = "180pi"
        for student in ("S=180π", "S = 180*pi", "S = 2π(6)(6+9) = 180π"):
            with self.subTest(student=student):
                is_correct, rule, _err = grade("algebra", student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={_err}")

    def test_numeric_with_units(self):
        for student in ("52.5", "52.5 N", "The answer is 52.5 newtons"):
            with self.subTest(student=student):
                is_correct, rule, _err = grade("numeric", student, "52.5")
                self.assertEqual(is_correct, 1, msg=f"rule={rule}")

    def test_algebra_latex_frac(self):
        is_correct, rule, _err = grade(
            "algebra", r"y = -\frac{3}{2}x + 7", "y=-3/2*x+7"
        )
        self.assertEqual(is_correct, 1, msg=f"rule={rule}")

    def test_mc_excluded_values_spacing(self):
        key = "Excluded values: -4 and x = -3"
        choices = (
            "Excluded values: -4 and x = -3|Excluded values: 4 and x = -3|"
            "Excluded values: -4 and x = 1|Excluded values: 4 and x = 1"
        )
        student = "Excluded values: -4 and x=-3"
        is_correct, _rule, _err = grade("mc", student, key, choices)
        self.assertEqual(is_correct, 1)

    def test_mc_trailing_punctuation(self):
        is_correct, _rule, _err = grade(
            "mc", "neither.", "neither", "parallel|perpendicular|neither"
        )
        self.assertEqual(is_correct, 1)

    def test_unicode_minus_normalized_for_grading(self):
        cases = [
            ("numeric", "-15/64", "−15/64"),
            ("numeric", "(-2,-30)", "(−2,−30)"),
            ("algebra", "2b-2", "2b−2"),
            ("numeric", "−5", "-5"),
        ]
        for answer_type, student, key in cases:
            with self.subTest(answer_type=answer_type, student=student, key=key):
                is_correct, rule, err = grade(answer_type, student, key)
                self.assertEqual(is_correct, 1, msg=f"rule={rule} err={err}")

    def test_unicode_minus_wrong_answer_still_fails(self):
        is_correct, _rule, _err = grade("numeric", "15/64", "−15/64")
        self.assertEqual(is_correct, 0)


class TestFipcRasch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ANCHORS_DIR.is_dir():
            raise unittest.SkipTest("anchor bundle not built")
        cls.bundle = load_anchor_bundle()

    def test_anchor_bundle_loads(self):
        self.assertGreaterEqual(len(self.bundle["informative_item_ids"]), 3)

    def test_fipc_estimates_new_item(self):
        anchor_items = self.bundle["items"]
        anchor_resp = self.bundle["responses"]
        anchor_params = self.bundle["params"]
        n_resp = len(anchor_resp)

        rng = np.random.default_rng(7)
        new_id = "NEW_HOLDOUT_TEST_1"
        b_new = 0.5
        thetas = np.linspace(-1.5, 1.5, n_resp)
        new_col = [1 if rng.random() < 1 / (1 + np.exp(-(t - b_new))) else 0 for t in thetas]

        new_items = pd.DataFrame(
            [
                {
                    "item_id": new_id,
                    "stem": "holdout test item",
                    "answer_key": "1",
                    "answer_type": "numeric",
                    "mc_choices": "",
                }
            ]
        )
        merged_items = pd.concat([anchor_items, new_items], ignore_index=True)
        new_resp = pd.DataFrame({new_id: new_col}, index=respondent_id_order(n_resp))
        merged_resp = pd.concat([anchor_resp, new_resp], axis=1)

        tmp = SERVICES_DIR / "anchors" / "_fipc_test"
        tmp.mkdir(exist_ok=True)
        resp_path = tmp / "responses.csv"
        params_out = tmp / "item_params.csv"
        fit_path = tmp / "fit_summary.json"
        anchor_params_path = tmp / "anchor_params.csv"

        merged_resp.reset_index(names="respondent_id").to_csv(resp_path, index=False)
        anchor_params.to_csv(anchor_params_path, index=False)

        run_rasch_fit(
            resp_path,
            params_out,
            fit_path,
            anchor_params_csv=anchor_params_path,
            new_item_ids=[new_id],
        )
        lookup = load_item_params_lookup(params_out)
        self.assertIn(new_id, lookup)
        self.assertIsNotNone(lookup[new_id]["difficulty_logit"])

    def test_holdout_anchor_recovery(self):
        """Hold one anchor out of fixed set; estimated logit should be near known value."""
        anchor_items = self.bundle["items"]
        anchor_resp = self.bundle["responses"]
        anchor_params = self.bundle["params"]

        holdout_id = anchor_items.iloc[0]["item_id"]
        known_b = float(
            anchor_params.loc[
                anchor_params["item_id"] == holdout_id, "difficulty_logit"
            ].iloc[0]
        )

        fixed_anchors = anchor_params[anchor_params["item_id"] != holdout_id]
        fit_items = anchor_items[anchor_items["item_id"] != holdout_id]
        fit_resp = anchor_resp.drop(columns=[holdout_id])
        holdout_resp = anchor_resp[[holdout_id]].rename(columns={holdout_id: "NEW_HOLDOUT"})

        merged_items = pd.concat(
            [
                fit_items,
                pd.DataFrame(
                    [
                        {
                            "item_id": "NEW_HOLDOUT",
                            "stem": anchor_items.iloc[0]["stem"],
                            "answer_key": anchor_items.iloc[0]["answer_key"],
                            "answer_type": anchor_items.iloc[0]["answer_type"],
                            "mc_choices": anchor_items.iloc[0].get("mc_choices", ""),
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
        merged_resp = pd.concat([fit_resp, holdout_resp.rename(columns={"NEW_HOLDOUT": "NEW_HOLDOUT"})], axis=1)

        tmp = SERVICES_DIR / "anchors" / "_holdout_test"
        tmp.mkdir(exist_ok=True)
        resp_path = tmp / "responses.csv"
        params_out = tmp / "item_params.csv"
        fit_path = tmp / "fit_summary.json"
        anchor_params_path = tmp / "anchor_params.csv"

        merged_resp.reset_index(names="respondent_id").to_csv(resp_path, index=False)
        fixed_anchors.to_csv(anchor_params_path, index=False)

        run_rasch_fit(
            resp_path,
            params_out,
            fit_path,
            anchor_params_csv=anchor_params_path,
            new_item_ids=["NEW_HOLDOUT"],
        )
        lookup = load_item_params_lookup(params_out)
        estimated = lookup["NEW_HOLDOUT"]["difficulty_logit"]
        self.assertAlmostEqual(estimated, known_b, delta=0.5)


class TestAnchorEnabled(unittest.TestCase):
    def test_anchor_mode_off_by_default(self):
        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": ""},
            clear=False,
        ):
            self.assertFalse(anchor_calibration_enabled())

    def test_anchor_mode_enabled_when_flag_on(self):
        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "1"},
            clear=False,
        ):
            self.assertTrue(anchor_calibration_enabled())


class TestFreeRaschDefaults(unittest.TestCase):
    def test_is_rasch_dof_error_detects_mirt_message(self):
        import subprocess

        exc = subprocess.CalledProcessError(
            1,
            "Rscript",
            stderr="Error: Too few degrees of freedom. There are only 3 degrees of freedom but 3 parameters were freely estimated.",
        )
        self.assertTrue(_is_rasch_dof_error(exc))

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.run_rasch_fit"
    )
    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_legacy_dof_failure_returns_insufficient_message(
        self, mock_generate: unittest.mock.MagicMock, mock_rasch: unittest.mock.MagicMock
    ):
        import subprocess

        questions = _sample_questions("cal_ok")
        n_resp = 20

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            col = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0] * 2
            pd.DataFrame(
                {"NEW_cal_ok_1": col[:n_resp]},
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen
        mock_rasch.side_effect = subprocess.CalledProcessError(
            1,
            "Rscript",
            stderr="Error: Too few degrees of freedom.",
        )

        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "0"},
            clear=False,
        ):
            _filtered, debug, meta = auto_calibrate_and_filter_questions(
                questions, min_logit=-3.0, max_logit=3.0, n_respondents=n_resp
            )

        self.assertEqual(meta["calibrationStatus"], CALIBRATION_INSUFFICIENT)
        self.assertEqual(meta["userMessage"], FREE_RASCH_INSUFFICIENT_MESSAGE)
        step = next(r for r in debug if r["rowType"] == "step")
        self.assertEqual(step["reason"], CALIBRATION_INSUFFICIENT)
        self.assertFalse(step["scored"])

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.run_rasch_fit"
    )
    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_free_rasch_zero_variance_all_correct_has_direction(
        self, mock_generate: unittest.mock.MagicMock, mock_rasch: unittest.mock.MagicMock
    ):
        questions = _two_step_questions("sat_easy")
        n_resp = 20
        informative_id = "NEW_sat_easy_1"
        saturated_id = "NEW_sat_easy_2"

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            varying = [1 if i % 2 == 0 else 0 for i in range(n_resp)]
            pd.DataFrame(
                {
                    informative_id: varying,
                    saturated_id: [1] * n_resp,
                },
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        def _fake_rasch(responses_csv, item_params_csv, fit_summary_json):
            pd.DataFrame(
                [
                    {
                        "item_id": informative_id,
                        "difficulty_logit": 0.0,
                        "se_difficulty": 0.2,
                    }
                ]
            ).to_csv(item_params_csv, index=False)
            Path(fit_summary_json).write_text("{}", encoding="utf-8")

        mock_generate.side_effect = _fake_gen
        mock_rasch.side_effect = _fake_rasch

        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "0"},
            clear=False,
        ):
            _filtered, debug, meta = auto_calibrate_and_filter_questions(
                questions, min_logit=-3.0, max_logit=3.0, n_respondents=n_resp
            )

        self.assertEqual(meta["calibrationStatus"], CALIBRATION_CALIBRATED)
        saturated = next(r for r in debug if r.get("itemId") == saturated_id)
        self.assertEqual(saturated["reason"], "zero_variance")
        self.assertFalse(saturated["scored"])
        self.assertEqual(saturated["saturationDirection"], "all_correct")

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.run_rasch_fit"
    )
    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_free_rasch_zero_variance_all_wrong_has_direction(
        self, mock_generate: unittest.mock.MagicMock, mock_rasch: unittest.mock.MagicMock
    ):
        questions = _two_step_questions("sat_hard")
        n_resp = 20
        informative_id = "NEW_sat_hard_1"
        saturated_id = "NEW_sat_hard_2"

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            varying = [1 if i % 2 == 0 else 0 for i in range(n_resp)]
            pd.DataFrame(
                {
                    informative_id: varying,
                    saturated_id: [0] * n_resp,
                },
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        def _fake_rasch(responses_csv, item_params_csv, fit_summary_json):
            pd.DataFrame(
                [
                    {
                        "item_id": informative_id,
                        "difficulty_logit": 0.5,
                        "se_difficulty": 0.2,
                    }
                ]
            ).to_csv(item_params_csv, index=False)
            Path(fit_summary_json).write_text("{}", encoding="utf-8")

        mock_generate.side_effect = _fake_gen
        mock_rasch.side_effect = _fake_rasch

        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "0"},
            clear=False,
        ):
            _filtered, debug, meta = auto_calibrate_and_filter_questions(
                questions, min_logit=-3.0, max_logit=3.0, n_respondents=n_resp
            )

        self.assertEqual(meta["calibrationStatus"], CALIBRATION_CALIBRATED)
        saturated = next(r for r in debug if r.get("itemId") == saturated_id)
        self.assertEqual(saturated["reason"], "zero_variance")
        self.assertFalse(saturated["scored"])
        self.assertEqual(saturated["saturationDirection"], "all_wrong")

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_insufficient_all_wrong_items_include_saturation_direction(
        self, mock_generate: unittest.mock.MagicMock
    ):
        questions = _five_step_questions("all_wrong")
        n_resp = 20
        item_ids = [f"NEW_all_wrong_{i}" for i in range(1, 6)]

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {item_id: [0] * n_resp for item_id in item_ids},
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "0"},
            clear=False,
        ):
            _filtered, debug, meta = auto_calibrate_and_filter_questions(
                questions, min_logit=-3.0, max_logit=3.0, n_respondents=n_resp
            )

        self.assertEqual(meta["calibrationStatus"], CALIBRATION_INSUFFICIENT)
        self.assertEqual(meta.get("nInformativeNewItems"), 0)
        step_evals = [r for r in debug if r["rowType"] == "step"]
        self.assertEqual(len(step_evals), 5)
        for row in step_evals:
            self.assertEqual(row["reason"], CALIBRATION_INSUFFICIENT)
            self.assertFalse(row["scored"])
            self.assertEqual(row["saturationDirection"], "all_wrong")

    @patch(
        "abscribe_backend.services.question_difficulty.difficulty_service.generate_responses_async_bounded"
    )
    def test_insufficient_mixed_saturation_directions_per_item(
        self, mock_generate: unittest.mock.MagicMock
    ):
        questions = _five_step_questions("mixed_sat")
        n_resp = 20
        easy_ids = ["NEW_mixed_sat_1", "NEW_mixed_sat_2"]
        hard_ids = ["NEW_mixed_sat_3", "NEW_mixed_sat_4", "NEW_mixed_sat_5"]
        columns = {
            item_id: ([1] * n_resp if item_id in easy_ids else [0] * n_resp)
            for item_id in easy_ids + hard_ids
        }

        async def _fake_gen(args):
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                columns,
                index=respondent_id_order(n_resp),
            ).reset_index(names="respondent_id").to_csv(out / "responses.csv", index=False)

        mock_generate.side_effect = _fake_gen

        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION": "0"},
            clear=False,
        ):
            _filtered, debug, meta = auto_calibrate_and_filter_questions(
                questions, min_logit=-3.0, max_logit=3.0, n_respondents=n_resp
            )

        self.assertEqual(meta["calibrationStatus"], CALIBRATION_INSUFFICIENT)
        by_item = {r["itemId"]: r for r in debug if r["rowType"] == "step"}
        for item_id in easy_ids:
            self.assertEqual(by_item[item_id]["saturationDirection"], "all_correct")
        for item_id in hard_ids:
            self.assertEqual(by_item[item_id]["saturationDirection"], "all_wrong")


if __name__ == "__main__":
    unittest.main()
