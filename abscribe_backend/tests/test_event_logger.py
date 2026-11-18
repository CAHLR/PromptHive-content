"""Unit tests for abscribe_backend.services.event_logger.

Required behaviours:

A. If no Firebase credentials are configured, ``init()`` must not raise and
   logging must stay off; subsequent ``log_*`` calls return ``None``.
B. With a fake Firestore client injected, a question-generation event lands
   in ``PromptHive/{env}/questionGenerations/{auto-id}`` with every shared
   metadata field present (including ``userPromptText`` / ``userPromptHash``).
C. If the fake Firestore client raises inside ``set()``, the write thread
   swallows the exception and the public helpers never raise to the caller.
D. Difficulty records prefer the cell counts from ``calibration_meta`` (set
   by the live Rasch calibration from the response matrix), and only fall
   back to item counts (renamed ``itemsTotal`` / ``itemsUnscored``) when the
   matrix is unavailable.
E. ``_resolve_app_version`` prefers ``PROMPTHIVE_APP_VERSION`` over git over
   ``.deployed-backend-commit`` and never raises.
F. ``_calibration_env_defaults`` falls back to ``DEFAULT_CALIBRATION_TEMPERATURE``
   (1.75) when the env var is unset.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_event_logger():
    """Load services.event_logger directly, bypassing abscribe_backend/__init__.py.

    The package's ``__init__`` eagerly imports ``abscribe_backend.app``, which
    pulls in Flask and friends; those are not needed to exercise the logger in
    isolation. Loading the module directly from its file path lets the suite
    run without the full web-app dependency stack.
    """
    root = Path(__file__).resolve().parents[1]  # abscribe_backend/
    module_path = root / "abscribe_backend" / "services" / "event_logger.py"
    # Preseed empty package shells so the module's "from __future__" + relative
    # imports resolve cleanly even when the real package __init__ fails.
    for pkg_name in (
        "abscribe_backend",
        "abscribe_backend.services",
    ):
        if pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            # Point the shells at the real package directories so other test
            # files that import sibling submodules (e.g. services.question_difficulty)
            # still resolve when this file is collected first.
            pkg.__path__ = (
                [str(root / "abscribe_backend" / "services")]
                if pkg_name.endswith(".services")
                else [str(root / "abscribe_backend")]
            )
            sys.modules[pkg_name] = pkg
            # Mirror what a real import does so attribute walks such as
            # mock.patch("abscribe_backend.services.x.y") find the child.
            parent_name, _, child = pkg_name.rpartition(".")
            if parent_name:
                setattr(sys.modules[parent_name], child, pkg)
    spec = importlib.util.spec_from_file_location(
        "abscribe_backend.services.event_logger", str(module_path)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["abscribe_backend.services.event_logger"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


event_logger = _load_event_logger()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeDocument:
    def __init__(self, parent: "_FakeCollection"):
        self._parent = parent

    def set(self, payload, **_kwargs):  # Firestore API
        self._parent.writes.append(dict(payload))


class _FakeCollection:
    def __init__(self, name: str, parent_path: str = ""):
        self.name = name
        self.parent_path = parent_path
        self.writes: list[dict] = []
        self._documents: dict[str, "_FakeDocument"] = {}
        self._child_collections: dict[str, "_FakeCollection"] = {}
        # Mirror writes to the ultimate root client for easy assertions.
        self.root_sink: list[dict] | None = None

    def document(self, doc_id: str | None = None) -> _FakeDocument:
        doc_id = doc_id or f"auto_{len(self._documents)}"
        if doc_id not in self._documents:
            self._documents[doc_id] = _FakeDocWithChildren(self, doc_id)
        return self._documents[doc_id]


class _FakeDocWithChildren(_FakeDocument):
    def __init__(self, parent: _FakeCollection, doc_id: str):
        super().__init__(parent)
        self.doc_id = doc_id
        self._collections: dict[str, _FakeCollection] = {}

    def collection(self, name: str) -> _FakeCollection:
        if name not in self._collections:
            child = _FakeCollection(name, parent_path=self.doc_id)
            child.root_sink = (
                self._parent.root_sink
                if self._parent.root_sink is not None
                else None
            )
            self._collections[name] = child
        return self._collections[name]

    def set(self, payload, **_kwargs):
        # Record both on the parent collection and on the root sink.
        super().set(payload, **_kwargs)
        if self._parent.root_sink is not None:
            self._parent.root_sink.append(dict(payload))


class FakeFirestoreClient:
    """Minimal fake with a `.collection()` entry point."""

    def __init__(self):
        self.writes: list[dict] = []
        self._root: dict[str, _FakeCollection] = {}

    def collection(self, name: str) -> _FakeCollection:
        if name not in self._root:
            col = _FakeCollection(name)
            col.root_sink = self.writes
            self._root[name] = col
        return self._root[name]


class RaisingFirestoreClient:
    """Fake whose underlying ``set`` always raises."""

    def collection(self, _name: str):
        class _Doc:
            def set(self, _payload, **_kwargs):
                raise RuntimeError("firestore unavailable")

            def collection(self, _n):
                return self

            def document(self, _id=None):
                return self

        return _Doc()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _reset_module():
    event_logger._reset_for_tests()


def _enable_with_fake(client, env="dev", app_version="testver"):
    _reset_module()
    event_logger._initialized = True
    event_logger._enabled = True
    event_logger._client = client
    event_logger._env = env
    event_logger._app_version = app_version
    event_logger._server_timestamp_sentinel = "SERVER_TIMESTAMP"
    from concurrent.futures import ThreadPoolExecutor

    event_logger._executor = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class InitWithoutCredentialsTests(unittest.TestCase):
    def tearDown(self):
        _reset_module()

    def test_init_without_credentials_leaves_logging_disabled(self):
        _reset_module()
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "FIREBASE_SERVICE_ACCOUNT_PATH",
                "GOOGLE_APPLICATION_CREDENTIALS",
            )
        }
        with patch.dict(os.environ, env, clear=True), self.assertLogs(
            "abscribe_backend.services.event_logger", level="WARNING"
        ) as captured:
            event_logger.init()

        self.assertFalse(event_logger._enabled)
        self.assertIsNone(event_logger._client)
        # init() must not raise and must emit exactly one warning.
        self.assertTrue(
            any("Firestore logging disabled" in m for m in captured.output),
            f"expected disabled-warning, got {captured.output!r}",
        )

        # log helpers must be no-ops that return None (never raise).
        self.assertIsNone(
            event_logger.log_question_generation(
                messages=[{"role": "system", "content": "hi"}],
                requested_model="gpt-4o",
                used_model="gpt-4o",
                model_fallback=False,
                model_fallback_reason=None,
                temperature=0.0,
                difficulty_filter={"questionsToGenerate": 2},
                questions=[],
                context={"generationRunId": "r1"},
                duration_ms=1.2,
            )
        )
        self.assertIsNone(
            event_logger.log_difficulty_estimation(
                source="estimate_button",
                questions=[],
                scores=[],
                n_respondents=5,
                calibration_meta={},
                context={"generationRunId": "r2"},
                duration_ms=0.5,
            )
        )


class QuestionGenerationWriteTests(unittest.TestCase):
    def tearDown(self):
        _reset_module()

    def test_question_generation_writes_all_shared_metadata(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev", app_version="abc1234")

        fut = event_logger.log_question_generation(
            messages=[
                {"role": "system", "content": "You are a tutor."},
                {"role": "user", "content": "Generate 3 problems."},
            ],
            requested_model="gpt-5.1",
            used_model="gpt-4o",
            model_fallback=True,
            model_fallback_reason="structured output not supported",
            temperature=0.3,
            difficulty_filter={
                "questionsToGenerate": 3,
                "skipCalibration": True,
            },
            questions=[
                {
                    "id": "q1",
                    "problemName": "P1",
                    "Body Text": "What is 2+2?",
                    "Answer": "4",
                    "answerType": "numeric",
                },
                {
                    "problemName": "P2",
                    "Body Text": "What is 3+3?",
                    "Answer": "6",
                    "answerType": "numeric",
                },
            ],
            context={
                "userId": "user_abc12",  # raw hintviewer_user_id seed
                "userName": "urban-wallaby",  # generateNameFromSeed(userId)
                "spreadsheetId": "sheet-xyz",
                "lessonName": "Lesson A",
                "generationRunId": "run-1",
                "clientTimestamp": 1_700_000_000_000,
                "useOriginalItemsAsTemplates": True,
                "useGeneratedItemsAsTemplates": False,
                "batchNumber": 2,
                "userPromptText": "Make 3 easy fraction problems.",
                "userPromptHash": None,  # exercise backend recomputation
                "scratchpadId": "sp_abcdef0123456789",
            },
            duration_ms=42.0,
        )
        self.assertIsNotNone(fut, "log_question_generation should return a Future")
        fut.result(timeout=5)
        # Also force the executor to drain, in case Firestore timing jitter.
        time.sleep(0.05)

        self.assertEqual(
            len(fake.writes), 1, f"expected one write, got {fake.writes!r}"
        )
        doc = fake.writes[0]

        # Shared metadata every event must carry.
        self.assertEqual(doc["eventType"], "question_generation")
        self.assertEqual(doc["environment"], "dev")
        self.assertEqual(doc["appVersion"], "abc1234")
        # userId = raw hintviewer_user_id seed (same key the prompt library
        # saves); userName = the display handle shown in the top bar.
        self.assertEqual(doc["userId"], "user_abc12")
        self.assertEqual(doc["userName"], "urban-wallaby")
        self.assertEqual(doc["spreadsheetId"], "sheet-xyz")
        self.assertEqual(doc["lessonName"], "Lesson A")
        self.assertEqual(doc["generationRunId"], "run-1")
        # scratchpadId joins the record to the scratchpad node saved right after.
        self.assertEqual(doc["scratchpadId"], "sp_abcdef0123456789")
        self.assertEqual(doc["clientTimestamp"], 1_700_000_000_000)
        self.assertEqual(doc["serverTimestamp"], "SERVER_TIMESTAMP")
        self.assertAlmostEqual(doc["durationMs"], 42.0)
        self.assertIsNone(doc["error"])
        self.assertEqual(len(doc["promptHash"]), 64)
        # userPromptText / userPromptHash: raw scratch-pad text copied through,
        # hash recomputed on the backend because the client sent None.
        self.assertEqual(doc["userPromptText"], "Make 3 easy fraction problems.")
        expected_hash = hashlib.sha256(
            "Make 3 easy fraction problems.".encode("utf-8")
        ).hexdigest()
        self.assertEqual(doc["userPromptHash"], expected_hash)

        # Event-specific fields.
        self.assertEqual(doc["modelRequested"], "gpt-5.1")
        self.assertEqual(doc["modelUsed"], "gpt-4o")
        self.assertTrue(doc["modelFallback"])
        self.assertEqual(
            doc["modelFallbackReason"], "structured output not supported"
        )
        self.assertEqual(doc["temperature"], 0.3)
        self.assertEqual(doc["questionsRequested"], 3)
        self.assertTrue(doc["skipCalibration"])
        self.assertTrue(doc["useOriginalItemsAsTemplates"])
        self.assertFalse(doc["useGeneratedItemsAsTemplates"])
        self.assertEqual(doc["batchNumber"], 2)
        self.assertEqual(doc["questionCount"], 2)
        self.assertEqual(doc["questions"][0]["problemName"], "P1")
        self.assertEqual(doc["questions"][0]["text"], "What is 2+2?")
        # Answer keys are stamped on every per-question row so reviewers can
        # audit a generation record without the sheet.
        self.assertEqual(doc["questions"][0]["answer"], "4")
        self.assertEqual(doc["questions"][0]["answerType"], "numeric")
        self.assertEqual(doc["questions"][1]["answer"], "6")
        self.assertEqual(doc["questions"][1]["answerType"], "numeric")

        # Prompt text present and truncated to limit.
        self.assertIn("Generate 3 problems", doc["promptText"])


class DifficultyEstimationWriteTests(unittest.TestCase):
    def tearDown(self):
        _reset_module()

    def test_difficulty_estimation_writes_item_results(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        fut = event_logger.log_difficulty_estimation(
            source="estimate_button",
            questions=[
                {
                    "Problem Name": "P1",
                    "Body Text": "Solve x+1=2",
                    "Answer": "1",
                    "answerType": "numeric",
                },
                {
                    "Problem Name": "P2",
                    "Body Text": "Solve x+2=5",
                    "Answer": "3",
                    "answerType": "numeric",
                },
            ],
            scores=[
                {
                    "index": 0,
                    "itemId": "P1_step1",
                    "problemName": "P1",
                    "difficultyLogit": -0.3,
                    "difficultyLabel": "Easy",
                    "scored": True,
                },
                {
                    "index": 1,
                    "itemId": "P2_step1",
                    "problemName": "P2",
                    "difficultyLogit": None,
                    "difficultyLabel": None,
                    "scored": False,
                    "reason": "no responses",
                },
            ],
            n_respondents=100,
            calibration_meta={
                "calibrationStatus": "done",
                "respondentTemperature": 1.5,
                "respondentAbility": [{"ability": 0.0}],
            },
            context={"generationRunId": "run-9", "scratchpadId": "sp_node_1"},
            duration_ms=1234.5,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)

        self.assertEqual(len(fake.writes), 1)
        doc = fake.writes[0]
        self.assertEqual(doc["eventType"], "difficulty_estimation")
        self.assertEqual(doc["source"], "estimate_button")
        self.assertEqual(doc["scratchpadId"], "sp_node_1")
        self.assertEqual(doc["nRespondents"], 100)
        self.assertEqual(doc["temperature"], 1.5)
        self.assertEqual(doc["calibrationStatus"], "done")
        # calibration_meta here has no response-matrix cells, so the logger
        # falls back to the row-level item counts and names them accordingly.
        self.assertNotIn("cellsTotal", doc)
        self.assertNotIn("cellsMissingCount", doc)
        self.assertEqual(doc["itemsTotal"], 2)
        self.assertEqual(doc["itemsUnscored"], 1)
        self.assertEqual(doc["itemsScored"], ["P1_step1"])
        self.assertEqual(len(doc["itemResults"]), 2)
        first = doc["itemResults"][0]
        self.assertEqual(first["id"], "P1_step1")
        self.assertEqual(first["problemName"], "P1")
        self.assertEqual(first["text"], "Solve x+1=2")
        # Answer + answerType copied from the matched question row so each
        # itemResult is self-contained for human review.
        self.assertEqual(first["answer"], "1")
        self.assertEqual(first["answerType"], "numeric")
        self.assertEqual(first["logit"], -0.3)
        self.assertEqual(first["band"], "Easy")
        self.assertTrue(first["scored"])
        second = doc["itemResults"][1]
        self.assertFalse(second["scored"])
        self.assertEqual(second["unscoredReason"], "no responses")
        self.assertEqual(second["answer"], "3")
        self.assertEqual(second["answerType"], "numeric")


class WriteFailureSwallowedTests(unittest.TestCase):
    def tearDown(self):
        _reset_module()

    def test_write_exception_is_swallowed_and_logged(self):
        _enable_with_fake(RaisingFirestoreClient(), env="dev")

        with self.assertLogs(
            "abscribe_backend.services.event_logger", level="WARNING"
        ) as captured:
            fut = event_logger.log_question_generation(
                messages=[{"role": "system", "content": "x"}],
                requested_model="gpt-4o",
                used_model="gpt-4o",
                model_fallback=False,
                model_fallback_reason=None,
                temperature=0.0,
                difficulty_filter={},
                questions=[],
                context={"generationRunId": "r"},
                duration_ms=0.1,
            )
            self.assertIsNotNone(fut)
            # The Future itself must not raise either — the thread swallowed it.
            self.assertIsNone(fut.result(timeout=5))

        # And the module logged a WARNING with the underlying message.
        self.assertTrue(
            any("firestore unavailable" in msg for msg in captured.output),
            f"expected underlying error in warning, got {captured.output!r}",
        )


class GrpcGeventHelperTests(unittest.TestCase):
    """The grpc-gevent helper must be a silent no-op when gevent is not patched
    (the normal test / script runtime) and must stay no-op on repeat calls
    (once-per-process guarantee).
    """

    def tearDown(self):
        _reset_module()

    def test_helper_is_no_op_when_gevent_not_patched(self):
        _reset_module()
        # In the regular unittest process, gevent monkey-patching is not
        # active, so is_module_patched("socket") returns False and the
        # helper must do nothing — importantly, it must not raise, must
        # not touch grpc, and must not flip _enabled.
        self.assertFalse(event_logger._gevent_init_done)
        with self.assertLogs(
            "abscribe_backend.services.event_logger", level="INFO"
        ) as captured:
            event_logger._maybe_init_grpc_gevent()
        self.assertTrue(event_logger._gevent_init_done)
        self.assertFalse(event_logger._enabled)
        self.assertIsNone(event_logger._client)
        self.assertTrue(
            any("grpc gevent init skipped" in m for m in captured.output),
            f"expected a 'skipped' INFO line, got {captured.output!r}",
        )

        # Second call must stay a no-op (no new log line, no exception).
        with patch.object(
            event_logger.logger, "info", side_effect=AssertionError(
                "helper logged on repeat call"
            )
        ):
            event_logger._maybe_init_grpc_gevent()  # must not raise


class CalibrationEnvDefaultsTests(unittest.TestCase):
    """Fix #1: logger fallback temperature must equal the respondent default
    (1.75), not the previous hard-coded 1.0."""

    def tearDown(self):
        _reset_module()

    def test_temperature_fallback_matches_respondent_default(self):
        # Unset the override so we exercise the fallback.
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE"
        }
        with patch.dict(os.environ, env, clear=True):
            defaults = event_logger._calibration_env_defaults()
        # Must be 1.75 — same as DEFAULT_CALIBRATION_TEMPERATURE in
        # services/question_difficulty/respondents.py. Any deviation means
        # the logger would record the wrong temperature when the service
        # could not stamp it on the returned meta.
        self.assertAlmostEqual(defaults["temperature"], 1.75)
        self.assertEqual(defaults["model"], "gpt-4o")
        self.assertEqual(defaults["token_cap"], 150)

    def test_temperature_env_override_wins(self):
        with patch.dict(
            os.environ,
            {"QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE": "0.7"},
            clear=False,
        ):
            defaults = event_logger._calibration_env_defaults()
        self.assertAlmostEqual(defaults["temperature"], 0.7)

    def test_difficulty_log_uses_fallback_temperature_when_meta_silent(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        env = {
            k: v
            for k, v in os.environ.items()
            if k != "QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE"
        }
        with patch.dict(os.environ, env, clear=True):
            fut = event_logger.log_difficulty_estimation(
                source="estimate_button",
                questions=[{"Problem Name": "P1", "Body Text": "Solve x+1=2"}],
                scores=[
                    {
                        "index": 0,
                        "itemId": "P1_step1",
                        "scored": True,
                        "difficultyLogit": 0.0,
                    }
                ],
                n_respondents=30,
                # meta explicitly carries no respondentTemperature so the
                # fallback path is exercised.
                calibration_meta={"calibrationStatus": "done"},
                context={"generationRunId": "r1"},
                duration_ms=1.0,
            )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)

        self.assertEqual(len(fake.writes), 1)
        self.assertAlmostEqual(fake.writes[0]["temperature"], 1.75)
        self.assertEqual(fake.writes[0]["tokenCap"], 150)


class DifficultyCellCountsTests(unittest.TestCase):
    """Fix #4: cellsTotal/cellsMissingCount come from the response matrix
    when the service provides them; otherwise the fields are renamed to
    itemsTotal/itemsUnscored so consumers know the number refers to item
    rows rather than response-matrix cells."""

    def tearDown(self):
        _reset_module()

    def test_cells_total_comes_from_calibration_meta_when_present(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        # 30 respondents * 25 items = 750 cells; 10 missing answers.
        fut = event_logger.log_difficulty_estimation(
            source="after_generation",
            questions=[{"Problem Name": f"P{i}"} for i in range(25)],
            scores=[
                {
                    "index": i,
                    "itemId": f"P{i}_step1",
                    "scored": True,
                    "difficultyLogit": 0.0,
                }
                for i in range(25)
            ],
            n_respondents=30,
            calibration_meta={
                "calibrationStatus": "done",
                "cellsTotal": 750,
                "cellsMissingCount": 10,
                "respondentTemperature": 1.75,
            },
            context={"generationRunId": "r"},
            duration_ms=1.0,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)

        doc = fake.writes[0]
        # Real response-matrix cell counts win; no itemsTotal fallback.
        self.assertEqual(doc["cellsTotal"], 750)
        self.assertEqual(doc["cellsMissingCount"], 10)
        self.assertNotIn("itemsTotal", doc)
        self.assertNotIn("itemsUnscored", doc)

    def test_falls_back_to_item_counts_when_matrix_absent(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        fut = event_logger.log_difficulty_estimation(
            source="estimate_button",
            questions=[
                {"Problem Name": "P1", "Body Text": "x"},
                {"Problem Name": "P2", "Body Text": "y"},
                {"Problem Name": "P3", "Body Text": "z"},
            ],
            scores=[
                {"index": 0, "itemId": "P1_step1", "scored": True,
                 "difficultyLogit": 0.0},
                {"index": 1, "itemId": "P2_step1", "scored": False,
                 "reason": "no responses"},
                {"index": 2, "itemId": "P3_step1", "scored": False,
                 "reason": "no responses"},
            ],
            n_respondents=5,
            # No cellsTotal / cellsMissingCount in meta.
            calibration_meta={"calibrationStatus": "insufficient"},
            context={"generationRunId": "r"},
            duration_ms=0.5,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)

        doc = fake.writes[0]
        self.assertNotIn("cellsTotal", doc)
        self.assertNotIn("cellsMissingCount", doc)
        self.assertEqual(doc["itemsTotal"], 3)
        self.assertEqual(doc["itemsUnscored"], 2)


class DifficultyUserPromptHashTests(unittest.TestCase):
    """Fix #3: difficulty records must carry the ``userPromptHash`` the
    frontend sent (when it sent one) so Generate + Estimate records on the
    same prompt share a stable join key."""

    def tearDown(self):
        _reset_module()

    def test_user_prompt_hash_from_context_passed_through(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        prompt_hash = (
            "a" * 64  # arbitrary deterministic 64-hex-char value
        )
        fut = event_logger.log_difficulty_estimation(
            source="after_generation",
            questions=[],
            scores=[],
            n_respondents=10,
            calibration_meta={"calibrationStatus": "done"},
            context={
                "generationRunId": "r",
                "userId": "user_abc12",
                "userName": "urban-wallaby",
                "userPromptHash": prompt_hash,
            },
            duration_ms=0.1,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)

        doc = fake.writes[0]
        self.assertEqual(doc["userPromptHash"], prompt_hash)
        # Raw seed + display name are both stamped on difficulty records too.
        self.assertEqual(doc["userId"], "user_abc12")
        self.assertEqual(doc["userName"], "urban-wallaby")

    def test_user_prompt_hash_is_none_when_frontend_omits_it(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        fut = event_logger.log_difficulty_estimation(
            source="estimate_button",
            questions=[],
            scores=[],
            n_respondents=1,
            calibration_meta={"calibrationStatus": "done"},
            context={"generationRunId": "r"},
            duration_ms=0.1,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)
        self.assertIsNone(fake.writes[0]["userPromptHash"])

    def test_scratchpad_id_is_null_when_absent_on_both_event_types(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        fut1 = event_logger.log_question_generation(
            messages=[{"role": "user", "content": "x"}],
            requested_model="gpt-4o",
            used_model="gpt-4o",
            model_fallback=False,
            model_fallback_reason=None,
            temperature=0.1,
            difficulty_filter=None,
            questions=[],
            context={"generationRunId": "r"},
            duration_ms=0.1,
        )
        fut2 = event_logger.log_difficulty_estimation(
            source="estimate_button",
            questions=[],
            scores=[],
            n_respondents=1,
            calibration_meta={"calibrationStatus": "done"},
            context=None,
            duration_ms=0.1,
        )
        for fut in (fut1, fut2):
            self.assertIsNotNone(fut)
            fut.result(timeout=5)
        self.assertEqual(len(fake.writes), 2)
        for doc in fake.writes:
            self.assertIn("scratchpadId", doc)
            self.assertIsNone(doc["scratchpadId"])


class UserPromptTextFallbackTests(unittest.TestCase):
    """When the client cannot send ``userPromptText`` (older frontend, or
    the scratchpad message carries only ``hidden: true`` by default), the
    backend must derive it from the user-role plain-text messages so every
    generation record for a run shares the same ``userPromptHash``.

    Vision-prompt image parts must be excluded from the derived text so the
    hash reflects only what the user wrote.
    """

    def tearDown(self):
        _reset_module()

    def test_backend_falls_back_to_user_messages_when_context_missing(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        messages = [
            {"role": "system", "content": "You are a tutor."},
            {"role": "user", "content": "Write 3 problems on average rate of change."},
        ]
        fut = event_logger.log_question_generation(
            messages=messages,
            requested_model="gpt-4o",
            used_model="gpt-4o",
            model_fallback=False,
            model_fallback_reason=None,
            temperature=0.0,
            difficulty_filter={},
            questions=[],
            # NO userPromptText / userPromptHash in context: exercise fallback.
            context={"generationRunId": "run-xyz"},
            duration_ms=0.1,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)
        doc = fake.writes[0]
        self.assertEqual(
            doc["userPromptText"],
            "Write 3 problems on average rate of change.",
        )
        expected = hashlib.sha256(
            "Write 3 problems on average rate of change.".encode("utf-8")
        ).hexdigest()
        self.assertEqual(doc["userPromptHash"], expected)

    def test_identical_hash_across_two_calls_in_same_run(self):
        """All generation records for the same scratch pad must carry the
        same userPromptHash, so Firestore can group them cleanly."""
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        shared_text = "Write new problems on average rate of change."
        for template_idx in range(3):
            fut = event_logger.log_question_generation(
                messages=[
                    {"role": "system", "content": f"Template {template_idx}"},
                    {"role": "user", "content": shared_text},
                ],
                requested_model="gpt-4o",
                used_model="gpt-4o",
                model_fallback=False,
                model_fallback_reason=None,
                temperature=0.0,
                difficulty_filter={},
                questions=[],
                context={"generationRunId": "run-same"},
                duration_ms=0.1,
            )
            fut.result(timeout=5)

        hashes = {w["userPromptHash"] for w in fake.writes}
        self.assertEqual(
            len(hashes), 1,
            f"expected one shared hash across generation records, got {hashes!r}",
        )

    def test_vision_image_url_parts_are_skipped(self):
        """Content arrays (vision messages) contribute only the text parts
        to the derived prompt — image_url entries are dropped."""
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Please solve this problem"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,SECRET"},
                    },
                ],
            },
        ]
        fut = event_logger.log_question_generation(
            messages=messages,
            requested_model="gpt-4o",
            used_model="gpt-4o",
            model_fallback=False,
            model_fallback_reason=None,
            temperature=0.0,
            difficulty_filter={},
            questions=[],
            context={"generationRunId": "run-vision"},
            duration_ms=0.1,
        )
        fut.result(timeout=5)
        doc = fake.writes[0]
        self.assertEqual(doc["userPromptText"], "Please solve this problem")
        self.assertNotIn("SECRET", doc["userPromptText"] or "")

    def test_client_supplied_text_wins_over_fallback(self):
        """When the client sends ``userPromptText`` the backend must use that
        verbatim and NOT re-derive from messages."""
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        fut = event_logger.log_question_generation(
            messages=[{"role": "user", "content": "fallback-text"}],
            requested_model="gpt-4o",
            used_model="gpt-4o",
            model_fallback=False,
            model_fallback_reason=None,
            temperature=0.0,
            difficulty_filter={},
            questions=[],
            context={
                "generationRunId": "r",
                "userPromptText": "client-supplied-text",
                "userPromptHash": "a" * 64,
            },
            duration_ms=0.1,
        )
        fut.result(timeout=5)
        doc = fake.writes[0]
        self.assertEqual(doc["userPromptText"], "client-supplied-text")
        self.assertEqual(doc["userPromptHash"], "a" * 64)


class AnswerKeyLoggingTests(unittest.TestCase):
    """Fix #2: generation records carry ``answer`` + ``answerType`` on every
    per-question row; difficulty records carry the same two fields on every
    ``itemResults`` row (resolved via ``score["index"]`` into the questions
    list)."""

    def tearDown(self):
        _reset_module()

    def test_generation_record_includes_answer_and_answer_type(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        fut = event_logger.log_question_generation(
            messages=[{"role": "user", "content": "x"}],
            requested_model="gpt-4o",
            used_model="gpt-4o",
            model_fallback=False,
            model_fallback_reason=None,
            temperature=0.0,
            difficulty_filter={},
            questions=[
                {
                    "problemName": "P1",
                    "Body Text": "Solve 2+2",
                    "Answer": "4",
                    "answerType": "numeric",
                },
                {
                    "problemName": "P2",
                    "Body Text": "Pick one",
                    "answer_key": "B",
                    "answer_type": "mc",
                },
            ],
            context={"generationRunId": "r"},
            duration_ms=0.1,
        )
        fut.result(timeout=5)
        doc = fake.writes[0]
        self.assertEqual(doc["questions"][0]["answer"], "4")
        self.assertEqual(doc["questions"][0]["answerType"], "numeric")
        # Snake_case variants (``answer_key`` / ``answer_type``) resolve too.
        self.assertEqual(doc["questions"][1]["answer"], "B")
        self.assertEqual(doc["questions"][1]["answerType"], "mc")

    def test_difficulty_item_results_include_answer_and_answer_type(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        fut = event_logger.log_difficulty_estimation(
            source="after_generation",
            questions=[
                {
                    "Problem Name": "P1",
                    "Body Text": "Solve 2+2",
                    "Answer": "4",
                    "answerType": "numeric",
                },
            ],
            scores=[
                {
                    "index": 0,
                    "itemId": "P1_step1",
                    "problemName": "P1",
                    "difficultyLogit": 0.0,
                    "scored": True,
                },
            ],
            n_respondents=5,
            calibration_meta={"calibrationStatus": "done"},
            context={"generationRunId": "r"},
            duration_ms=0.1,
        )
        fut.result(timeout=5)
        row = fake.writes[0]["itemResults"][0]
        self.assertEqual(row["answer"], "4")
        self.assertEqual(row["answerType"], "numeric")

    def test_missing_answer_key_logs_none_not_missing_field(self):
        """When a question dict omits ``Answer`` entirely, the field must
        still land in Firestore as ``None`` (schema stability)."""
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")
        fut = event_logger.log_question_generation(
            messages=[{"role": "user", "content": "x"}],
            requested_model="gpt-4o",
            used_model="gpt-4o",
            model_fallback=False,
            model_fallback_reason=None,
            temperature=0.0,
            difficulty_filter={},
            questions=[{"problemName": "P1", "Body Text": "no answer here"}],
            context={"generationRunId": "r"},
            duration_ms=0.1,
        )
        fut.result(timeout=5)
        row = fake.writes[0]["questions"][0]
        self.assertIn("answer", row)
        self.assertIn("answerType", row)
        self.assertIsNone(row["answer"])
        self.assertIsNone(row["answerType"])


class UserIdentityFieldsTests(unittest.TestCase):
    """Every event records BOTH the raw ``userId`` (same identifier the
    prompt library saves) AND the derived display ``userName``, so Firestore
    rows join cleanly to the library and still render a human handle on
    dashboards. Missing values must land as ``None`` (never a string
    ``"undefined"``) so downstream filters can tell the difference."""

    def tearDown(self):
        _reset_module()

    def test_both_user_fields_are_recorded_independently(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        fut = event_logger.log_question_generation(
            messages=[{"role": "user", "content": "hi"}],
            requested_model="gpt-4o",
            used_model="gpt-4o",
            model_fallback=False,
            model_fallback_reason=None,
            temperature=0.0,
            difficulty_filter={},
            questions=[],
            context={
                "userId": "user_abc12",
                "userName": "urban-wallaby",
                "generationRunId": "r",
            },
            duration_ms=0.1,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)

        doc = fake.writes[0]
        self.assertEqual(doc["userId"], "user_abc12")
        self.assertEqual(doc["userName"], "urban-wallaby")

    def test_user_fields_default_to_none_when_context_omits_them(self):
        fake = FakeFirestoreClient()
        _enable_with_fake(fake, env="dev")

        fut = event_logger.log_difficulty_estimation(
            source="estimate_button",
            questions=[],
            scores=[],
            n_respondents=1,
            calibration_meta={"calibrationStatus": "done"},
            context={"generationRunId": "r"},
            duration_ms=0.1,
        )
        self.assertIsNotNone(fut)
        fut.result(timeout=5)

        doc = fake.writes[0]
        # Fields must still be present (as ``None``) so Firestore schema
        # queries don't need special-case handling for older records.
        self.assertIn("userId", doc)
        self.assertIn("userName", doc)
        self.assertIsNone(doc["userId"])
        self.assertIsNone(doc["userName"])


class AppVersionResolutionTests(unittest.TestCase):
    """Fix #5: _resolve_app_version priority = env var > git > the first
    line of ``.deployed-backend-commit`` in the PromptHive folder one level
    above abscribe_backend. Never raises, returns None on total failure."""

    def tearDown(self):
        _reset_module()

    def test_env_var_wins(self):
        with patch.dict(
            os.environ,
            {"PROMPTHIVE_APP_VERSION": "env_hash_1"},
            clear=False,
        ), patch(
            "subprocess.check_output",
            side_effect=AssertionError(
                "git must not be called when env var is set"
            ),
        ):
            self.assertEqual(
                event_logger._resolve_app_version(), "env_hash_1"
            )

    def test_git_second_priority(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "PROMPTHIVE_APP_VERSION"
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "subprocess.check_output", return_value="git_short_hash\n"
        ):
            self.assertEqual(
                event_logger._resolve_app_version(), "git_short_hash"
            )

    def test_deployed_backend_commit_file_last_priority(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "PROMPTHIVE_APP_VERSION"
        }
        # Fake the module layout:
        #   <tmp>/PromptHive/abscribe_backend/abscribe_backend/services/event_logger.py
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            prompthive_dir = tmp_root / "PromptHive"
            services_dir = (
                prompthive_dir / "abscribe_backend" / "abscribe_backend" / "services"
            )
            services_dir.mkdir(parents=True)
            fake_module = services_dir / "event_logger.py"
            fake_module.write_text("# fake module location for test\n")
            deploy_file = prompthive_dir / ".deployed-backend-commit"
            deploy_file.write_text(
                "deploy_hash_abc\nignored second line\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, env, clear=True), patch(
                "subprocess.check_output",
                side_effect=OSError("no git in test sandbox"),
            ), patch.object(event_logger, "__file__", str(fake_module)):
                resolved = event_logger._resolve_app_version()
        self.assertEqual(resolved, "deploy_hash_abc")

    def test_returns_none_when_all_sources_empty(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "PROMPTHIVE_APP_VERSION"
        }
        with tempfile.TemporaryDirectory() as tmp:
            services_dir = (
                Path(tmp) / "PromptHive" / "abscribe_backend"
                / "abscribe_backend" / "services"
            )
            services_dir.mkdir(parents=True)
            fake_module = services_dir / "event_logger.py"
            fake_module.write_text("# fake\n")
            # No .deployed-backend-commit file; subprocess raises.
            with patch.dict(os.environ, env, clear=True), patch(
                "subprocess.check_output",
                side_effect=OSError("no git"),
            ), patch.object(event_logger, "__file__", str(fake_module)):
                self.assertIsNone(event_logger._resolve_app_version())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
