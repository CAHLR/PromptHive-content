"""Tests for the optional PROMPTHIVE_ACCESS_CODE gate on /chatGPT/ and /api/."""

import json
import unittest

import abscribe_backend.app as app_module
from abscribe_backend.app import app


class TestAccessCode(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self._original_code = app_module.PROMPTHIVE_ACCESS_CODE

    def tearDown(self):
        app_module.PROMPTHIVE_ACCESS_CODE = self._original_code

    def test_correct_header_passes(self):
        app_module.PROMPTHIVE_ACCESS_CODE = "test-secret-code"
        response = self.app.get(
            "/api/prompt-library/active",
            headers={"X-PromptHive-Access-Code": "test-secret-code"},
        )
        self.assertNotEqual(response.status_code, 401)
        if response.is_json:
            body = response.get_json()
            self.assertNotEqual(body, {"error": "access code required"})

    def test_missing_header_gives_401(self):
        app_module.PROMPTHIVE_ACCESS_CODE = "test-secret-code"
        response = self.app.get("/api/prompt-library/active")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            json.loads(response.data), {"error": "access code required"}
        )

    def test_wrong_header_gives_401(self):
        app_module.PROMPTHIVE_ACCESS_CODE = "test-secret-code"
        response = self.app.get(
            "/api/prompt-library/active",
            headers={"X-PromptHive-Access-Code": "wrong-code"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            json.loads(response.data), {"error": "access code required"}
        )

    def test_options_passes_without_header(self):
        app_module.PROMPTHIVE_ACCESS_CODE = "test-secret-code"
        response = self.app.options("/api/prompt-library/active")
        self.assertNotEqual(response.status_code, 401)

    def test_path_outside_chatGPT_and_api_passes_without_header(self):
        app_module.PROMPTHIVE_ACCESS_CODE = "test-secret-code"
        response = self.app.get("/documents")
        self.assertNotEqual(response.status_code, 401)
        if response.is_json:
            body = response.get_json()
            self.assertNotEqual(body, {"error": "access code required"})

    def test_check_off_when_access_code_unset(self):
        app_module.PROMPTHIVE_ACCESS_CODE = ""
        response = self.app.get("/api/prompt-library/active")
        self.assertNotEqual(response.status_code, 401)
        if response.is_json:
            body = response.get_json()
            self.assertNotEqual(body, {"error": "access code required"})

    def test_course_materials_not_gated_by_access_code(self):
        # Course Materials has its own instructor sign-in and never sends the
        # PromptHive access code, so the shared gate must let it through.
        app_module.PROMPTHIVE_ACCESS_CODE = "test-secret-code"
        response = self.app.get("/api/course-materials/catalog")
        if response.is_json:
            self.assertNotEqual(
                response.get_json(), {"error": "access code required"}
            )


if __name__ == "__main__":
    unittest.main()
