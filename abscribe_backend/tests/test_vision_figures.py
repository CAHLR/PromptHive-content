"""Tests for vision figure fetch / multimodal respondent prompts."""

from __future__ import annotations

import base64
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from abscribe_backend.services.question_difficulty.difficulty_service import (
    _oatutor_questions_to_items_df,
    filter_questions_by_difficulty_range,
    CALIBRATION_CALIBRATED,
    CALIBRATION_INSUFFICIENT,
)
from abscribe_backend.services.question_difficulty.generate_responses import (
    render_user_prompt,
)
from abscribe_backend.services.question_difficulty.vision_figures import (
    FigureCache,
    build_multimodal_user_content,
    figures_summary_line,
    is_vision_safe_image_data_url,
    parse_image_urls,
    prepare_items_for_vision,
)


class TestParseImageUrls(unittest.TestCase):
    def test_extracts_https_urls_from_images_column(self):
        raw = (
            "https://openstax.org/apps/archive/x/resources/abc "
            "https://cdn.example.com/fig.png"
        )
        urls = parse_image_urls(raw)
        self.assertEqual(len(urls), 2)

    def test_ignores_non_http_tokens(self):
        self.assertEqual(parse_image_urls("##cylinder.png relative/path.jpg"), [])

    def test_empty(self):
        self.assertEqual(parse_image_urls(""), [])
        self.assertEqual(parse_image_urls(None), [])


class TestDataUrlValidation(unittest.TestCase):
    def test_accepts_four_allowed_types(self):
        for mime in ("png", "jpeg", "jpg", "gif", "webp"):
            data = f"data:image/{mime};base64,aaaa"
            self.assertTrue(is_vision_safe_image_data_url(data), mime)

    def test_rejects_others(self):
        self.assertFalse(is_vision_safe_image_data_url("data:image/svg+xml;base64,aa"))


class TestFigureFetchFailKeepsItemLoud(unittest.TestCase):
    """(a) Figure fails -> text calibrate, figureAttached=false, concrete error."""

    def test_unfetchable_figure_keeps_item_with_loud_status(self):
        items_df = pd.DataFrame(
            [
                {
                    "item_id": "NEW_real11_1",
                    "stem": "Find area",
                    "answer_key": "180pi",
                    "answer_type": "algebra",
                    "mc_choices": "",
                    "image_urls": "https://example.invalid/missing.png",
                }
            ]
        )
        cache = FigureCache()
        with patch.object(
            cache,
            "_fetch",
            return_value=(None, "SSL error: certificate verify failed"),
        ):
            (
                filtered,
                index_to_item_id,
                index_to_skip_reason,
                _,
                figure_status,
            ) = prepare_items_for_vision(
                items_df,
                {1: "NEW_real11_1"},
                {"NEW_real11_1": 1},
                {},
                cache=cache,
            )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(index_to_item_id[1], "NEW_real11_1")
        self.assertEqual(index_to_skip_reason, {})
        status = figure_status["NEW_real11_1"]
        self.assertFalse(status["figureAttached"])
        self.assertEqual(status["figureCount"], 0)
        self.assertIn("SSL error", status["figureError"])
        self.assertEqual(figures_summary_line(figure_status), "figures: 0 attached, 1 failed")

        questions = [
            {
                "Problem Name": "real11",
                "Row Type": "problem",
                "Title": "Using a Formula",
                "Body Text": "cylinder",
                "Answer": "",
                "answerType": "",
                "Images": "https://example.invalid/missing.png",
            },
            {
                "Problem Name": "real11",
                "Row Type": "step",
                "Title": "Find SA",
                "Body Text": "",
                "Answer": "180pi",
                "answerType": "algebra",
                "Images": "",
            },
        ]
        _, debug, _ = filter_questions_by_difficulty_range(
            questions,
            -4.0,
            4.0,
            params_lookup={
                "NEW_real11_1": {"difficulty_logit": 0.4, "se_difficulty": 0.2}
            },
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status,
            calibration_status=CALIBRATION_CALIBRATED,
            include_unscored=True,
        )
        step = next(r for r in debug if str(r.get("rowType")).lower() == "step")
        self.assertEqual(step["itemId"], "NEW_real11_1")
        self.assertIs(step["figureAttached"], False)
        self.assertEqual(step["figureCount"], 0)
        self.assertIn("SSL error", step["figureError"])
        self.assertEqual(step.get("calibrationKey"), "180pi")
        self.assertTrue(step.get("figureUnavailable"))


class TestFigureFetchSuccessMultimodal(unittest.TestCase):
    """(b) Figure succeeds -> figureAttached=true, multimodal message."""

    def test_fetch_success_builds_multimodal(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode(
            "ascii"
        )
        items_df = pd.DataFrame(
            [
                {
                    "item_id": "NEW_real11_1",
                    "stem": "Find area",
                    "answer_key": "180pi",
                    "answer_type": "algebra",
                    "mc_choices": "",
                    "image_urls": "https://example.com/cyl.png",
                }
            ]
        )
        cache = FigureCache()
        with patch.object(cache, "_fetch", return_value=(data_url, None)):
            filtered, imap, skip, out_cache, fig_status = prepare_items_for_vision(
                items_df,
                {1: "NEW_real11_1"},
                {"NEW_real11_1": 1},
                {},
                cache=cache,
            )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(imap[1], "NEW_real11_1")
        self.assertTrue(fig_status["NEW_real11_1"]["figureAttached"])
        self.assertEqual(fig_status["NEW_real11_1"]["figureCount"], 1)
        self.assertIsNone(fig_status["NEW_real11_1"]["figureError"])
        self.assertEqual(figures_summary_line(fig_status), "figures: 1 attached, 0 failed")
        prompt = render_user_prompt(filtered.iloc[0].to_dict(), out_cache)
        self.assertIsInstance(prompt, list)
        self.assertIn("image_url", [p["type"] for p in prompt])


class TestNoFiguresNormal(unittest.TestCase):
    """(c) No figures -> no figure markings on evaluation."""

    def test_text_only_item_omits_figure_fields(self):
        items_df = pd.DataFrame(
            [
                {
                    "item_id": "NEW_plain_1",
                    "stem": "Solve 2+2",
                    "answer_key": "4",
                    "answer_type": "numeric",
                    "mc_choices": "",
                    "image_urls": "",
                }
            ]
        )
        filtered, imap, skip, _, fig_status = prepare_items_for_vision(
            items_df,
            {1: "NEW_plain_1"},
            {"NEW_plain_1": 1},
            {},
        )
        self.assertEqual(fig_status, {})
        self.assertEqual(figures_summary_line(fig_status), "figures: 0 attached, 0 failed")

        questions = [
            {
                "Problem Name": "plain",
                "Row Type": "problem",
                "Title": "T",
                "Body Text": "B",
                "Answer": "",
                "answerType": "",
                "Images": "",
            },
            {
                "Problem Name": "plain",
                "Row Type": "step",
                "Title": "",
                "Body Text": "Solve 2+2",
                "Answer": "4",
                "answerType": "numeric",
                "Images": "",
            },
        ]
        _, debug, _ = filter_questions_by_difficulty_range(
            questions,
            -4.0,
            4.0,
            params_lookup={
                "NEW_plain_1": {"difficulty_logit": -1.0, "se_difficulty": 0.1}
            },
            index_to_item_id={0: None, 1: "NEW_plain_1"},
            figure_status_by_item_id=fig_status,
            calibration_status=CALIBRATION_CALIBRATED,
            include_unscored=True,
        )
        step = next(r for r in debug if str(r.get("rowType")).lower() == "step")
        self.assertNotIn("figureAttached", step)
        self.assertNotIn("figureCount", step)
        self.assertNotIn("figureError", step)


class TestEmptyAnswerStillNotCalibratable(unittest.TestCase):
    def test_empty_answer_not_calibratable(self):
        questions = [
            {
                "Problem Name": "real11",
                "Row Type": "problem",
                "Title": "Using a Formula",
                "Body Text": "cylinder",
                "Answer": "",
                "answerType": "",
                "Images": "https://example.com/a.png",
            },
            {
                "Problem Name": "real11",
                "Row Type": "step",
                "Title": "Find SA",
                "Body Text": "",
                "Answer": "",
                "answerType": "algebra",
                "Images": "",
            },
        ]
        items_df, imap, _, skip = _oatutor_questions_to_items_df(
            questions, use_title_stem_fallback=True
        )
        self.assertTrue(items_df.empty)
        self.assertIsNone(imap[1])
        _, debug, _ = filter_questions_by_difficulty_range(
            questions,
            -4.0,
            4.0,
            params_lookup={},
            index_to_item_id=imap,
            index_to_skip_reason=skip,
            calibration_status=CALIBRATION_INSUFFICIENT,
            include_unscored=True,
        )
        step = next(r for r in debug if str(r.get("rowType")).lower() == "step")
        self.assertEqual(step["reason"], "not_calibratable")
        self.assertIsNone(step["itemId"])


class TestFigureCacheReuse(unittest.TestCase):
    def test_cache_fetches_once(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        cache = FigureCache()
        fetch = MagicMock(return_value=(data_url, None))
        with patch.object(cache, "_fetch", fetch):
            url = "https://example.com/once.png"
            self.assertEqual(cache.get(url), data_url)
            self.assertEqual(cache.get(url), data_url)
            self.assertEqual(fetch.call_count, 1)


class TestBuildMultimodal(unittest.TestCase):
    def test_rejects_unsafe_urls_silently(self):
        content = build_multimodal_user_content(
            "Q",
            ["data:image/svg+xml;base64,aa", "not-a-data-url"],
        )
        self.assertEqual(content, "Q")

    def test_item_with_figure_builds_multimodal_message(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        cache = FigureCache()
        cache.load_dict({"https://example.com/a.png": data_url})
        item = {
            "stem": "Find the surface area.",
            "answer_type": "algebra",
            "answer_key": "180\\pi",
            "image_urls": "https://example.com/a.png",
        }
        prompt = render_user_prompt(item, cache)
        self.assertIsInstance(prompt, list)
        self.assertIn("image_url", [p.get("type") for p in prompt])


if __name__ == "__main__":
    unittest.main()
