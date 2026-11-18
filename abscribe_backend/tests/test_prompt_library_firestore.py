"""Unit tests for services/prompt_library_firestore.py.

Uses an in-memory fake Firestore client (same approach as
tests/test_event_logger.py) so no network, credentials, or the real
google-cloud-firestore library are needed.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import itertools
import sys
import types
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Module loading (bypass abscribe_backend/__init__.py, which imports the app)
# ---------------------------------------------------------------------------
def _load_module(dotted: str, rel_path: str):
    """Always load a *fresh* copy of the module from its file.

    Other test files (test_api.py via the real package, test_event_logger.py
    via its own loader) may already have put different copies of these
    modules into ``sys.modules``. Reusing them would bind the library under
    test to a different ``event_logger`` instance than the one this file
    enables, so we always exec fresh copies in dependency order and point the
    package attribute at them so ``from ... import event_logger`` resolves to
    ours.
    """
    root = Path(__file__).resolve().parents[1]  # abscribe_backend/
    for pkg_name in ("abscribe_backend", "abscribe_backend.services"):
        if pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = (
                [str(root / "abscribe_backend" / "services")]
                if pkg_name.endswith(".services")
                else [str(root / "abscribe_backend")]
            )
            sys.modules[pkg_name] = pkg
            # Mirror a real import so attribute walks (mock.patch targets in
            # other test files) find the child package on its parent.
            parent_name, _, child = pkg_name.rpartition(".")
            if parent_name:
                setattr(sys.modules[parent_name], child, pkg)
    spec = importlib.util.spec_from_file_location(dotted, str(root / rel_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    parent_name, _, attr = dotted.rpartition(".")
    setattr(sys.modules[parent_name], attr, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


event_logger = _load_module(
    "abscribe_backend.services.event_logger",
    "abscribe_backend/services/event_logger.py",
)
lib = _load_module(
    "abscribe_backend.services.prompt_library_firestore",
    "abscribe_backend/services/prompt_library_firestore.py",
)


# ---------------------------------------------------------------------------
# Fake Firestore
# ---------------------------------------------------------------------------
class FakeArrayUnion:
    def __init__(self, values):
        self.values = list(values)


SERVER_TS = object()
_clock = itertools.count()


def _server_now() -> dt.datetime:
    # Monotonic so that dateCreated ordering is deterministic in tests.
    return dt.datetime(2026, 10, 12, 0, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(
        seconds=next(_clock)
    )


def _apply(target: dict, fields: dict) -> None:
    for key, value in fields.items():
        if isinstance(value, FakeArrayUnion):
            current = list(target.get(key) or [])
            for v in value.values:
                if v not in current:
                    current.append(v)
            target[key] = current
        elif value is SERVER_TS:
            target[key] = _server_now()
        else:
            target[key] = value


class FakeSnapshot:
    def __init__(self, ref: "FakeDocRef", data: dict | None):
        self.reference = ref
        self.id = ref.id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, collection: "FakeCollection", doc_id: str):
        self._collection = collection
        self.id = doc_id
        self.path = f"{collection.path}/{doc_id}"
        self._subcollections: dict[str, FakeCollection] = {}

    # Sub-collections (PromptHive/{env}/promptHints)
    def collection(self, name: str) -> "FakeCollection":
        if name not in self._subcollections:
            self._subcollections[name] = FakeCollection(
                self._collection.client, f"{self.path}/{name}"
            )
        return self._subcollections[name]

    def get(self, **kwargs) -> FakeSnapshot:
        self._collection.client.calls.append(("get", self.path, kwargs))
        return FakeSnapshot(self, self._collection.docs.get(self.id))

    def set(self, payload: dict, **kwargs) -> None:
        self._collection.client.calls.append(("set", self.path, kwargs))
        data: dict = {}
        _apply(data, payload)
        self._collection.docs[self.id] = data

    def update(self, fields: dict, **kwargs) -> None:
        self._collection.client.calls.append(("update", self.path, kwargs))
        if self.id not in self._collection.docs:
            raise RuntimeError(f"NotFound: {self.path}")
        _apply(self._collection.docs[self.id], fields)


class FakeQuery:
    def __init__(self, collection: "FakeCollection", filters: list):
        self._collection = collection
        self._filters = filters

    def where(self, *args, **kwargs) -> "FakeQuery":
        if "filter" in kwargs:  # FieldFilter form
            f = kwargs["filter"]
            field, op, value = f.field_path, f.op_string, f.value
        else:  # positional form
            field, op, value = args
        assert op == "=="
        return FakeQuery(self._collection, self._filters + [(field, value)])

    def stream(self, **kwargs):
        self._collection.client.calls.append(
            ("stream", self._collection.path, dict(kwargs, filters=list(self._filters)))
        )
        for doc_id, data in list(self._collection.docs.items()):
            if all(data.get(field) == value for field, value in self._filters):
                yield FakeSnapshot(self._collection.document(doc_id), data)


class FakeCollection:
    def __init__(self, client: "FakeFirestoreClient", path: str):
        self.client = client
        self.path = path
        self.docs: dict[str, dict] = {}
        self._refs: dict[str, FakeDocRef] = {}
        self._auto = itertools.count(1)

    def document(self, doc_id: str | None = None) -> FakeDocRef:
        if doc_id is None:
            doc_id = f"auto_{next(self._auto)}"
        # Cache refs so sub-collections hang off a stable parent document,
        # like the real client where paths are the identity.
        if doc_id not in self._refs:
            self._refs[doc_id] = FakeDocRef(self, doc_id)
        return self._refs[doc_id]

    def where(self, *args, **kwargs) -> FakeQuery:
        return FakeQuery(self, []).where(*args, **kwargs)

    def stream(self, **kwargs):
        return FakeQuery(self, []).stream(**kwargs)


class FakeBatch:
    def __init__(self, client: "FakeFirestoreClient"):
        self.client = client
        self.ops: list = []
        self.committed = False

    def set(self, ref: FakeDocRef, payload: dict, **kwargs) -> None:
        self.ops.append(("set", ref, payload))

    def update(self, ref: FakeDocRef, fields: dict) -> None:
        self.ops.append(("update", ref, fields))

    def commit(self, **kwargs):
        self.client.calls.append(("batch.commit", len(self.ops), kwargs))
        # Apply queued writes locally; the commit above is the only network call.
        for op, ref, payload in self.ops:
            if op == "set":
                data: dict = {}
                _apply(data, payload)
                ref._collection.docs[ref.id] = data
            else:
                if ref.id not in ref._collection.docs:
                    raise RuntimeError(f"NotFound: {ref.path}")
                _apply(ref._collection.docs[ref.id], payload)
        self.committed = True
        self.client.committed_batches.append(self)
        return []


class FakeFirestoreClient:
    def __init__(self):
        self._roots: dict[str, FakeCollection] = {}
        self.calls: list = []
        self.committed_batches: list[FakeBatch] = []

    def collection(self, name: str) -> FakeCollection:
        if name not in self._roots:
            self._roots[name] = FakeCollection(self, name)
        return self._roots[name]

    def batch(self) -> FakeBatch:
        return FakeBatch(self)

    # Convenience for assertions
    def hints(self, env="dev") -> FakeCollection:
        return self.collection("PromptHive").document(env).collection("promptHints")

    def scratchpads(self, env="dev") -> FakeCollection:
        return self.collection("PromptHive").document(env).collection("scratchpadPromptHints")


class FakeFieldFilter:
    def __init__(self, field_path, op_string, value):
        self.field_path, self.op_string, self.value = field_path, op_string, value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SERVER_APP_VERSION = "srv1234"


def _enable(client, env="dev", app_version=SERVER_APP_VERSION):
    event_logger._reset_for_tests()
    event_logger._initialized = True
    event_logger._enabled = True
    event_logger._client = client
    event_logger._env = env
    event_logger._app_version = app_version


def _disable():
    event_logger._reset_for_tests()


def _messages(text="Write 3 questions"):
    return [
        {
            "content": text,
            "hidden": True,
            "role": "user",
            "disabled": False,
            "id": "message_abc12",
            "time_created": "2026-10-12T00:00:00.000Z",
            "time_last_updated": "2026-10-12T00:00:01.000Z",
            "extraneous": "dropped",
        }
    ]


def _hint_payload(**overrides):
    base = {
        "userMessages": _messages(),
        "userId": "user-1",
        "lessonName": None,
        "isTextbookLevel": True,
        "spreadsheetId": "sheet-A",
        "lessonsTested": ["Lesson 1"],
        "temperature": 0.7,
        "model": "gpt-4o",
        "likes": 0,
        "timestamp": "ignored",
        "systemMessages": [{"role": "system", "content": "ignored"}],
    }
    base.update(overrides)
    return base


def _scratchpad_payload(**overrides):
    base = {
        "scratchpadId": "sp-1",
        "userMessages": _messages(),
        "userId": "user-1",
        "lessonName": None,
        "isTextbookLevel": True,
        "spreadsheetId": "sheet-A",
        "lessonsTested": ["Lesson 1"],
    }
    base.update(overrides)
    return base


EXPECTED_PROMPT_HINT_KEYS = {
    "id", "userMessages", "userId", "lessonName", "spreadsheetId", "status",
    "isTextbookLevel", "likes", "lessonsTested", "dateCreated", "scratchpadId",
    "temperature", "model",
}
EXPECTED_SCRATCHPAD_KEYS = {
    "scratchpadId", "userMessages", "userId", "lessonName", "spreadsheetId",
    "status", "isTextbookLevel", "lessonsTested", "dateCreated",
}
EXPECTED_STORED_HINT_FIELDS = {
    "userMessages", "userId", "userName", "lessonName", "spreadsheetId", "status",
    "isTextbookLevel", "lessonsTested", "likedUsers", "dateCreated", "parentId",
    "childrenIds", "parentMissing", "scratchpadId", "temperature", "model",
    "environment", "appVersion", "userPromptHash", "userPromptText",
    "generationRunId",
}
EXPECTED_STORED_SCRATCHPAD_FIELDS = {
    "scratchpadId", "userMessages", "userId", "userName", "lessonName", "spreadsheetId",
    "status", "isTextbookLevel", "lessonsTested", "dateCreated",
    "parentScratchpadId", "childrenScratchpadIds", "committedPromptIds",
    "parentMissing", "environment", "appVersion", "userPromptHash",
    "userPromptText", "generationRunId",
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        _enable(self.client, env="dev")
        self._orig_sentinels = (lib._ARRAY_UNION, lib._SERVER_TIMESTAMP)
        lib._ARRAY_UNION = FakeArrayUnion
        lib._SERVER_TIMESTAMP = SERVER_TS

    def tearDown(self):
        lib._ARRAY_UNION, lib._SERVER_TIMESTAMP = self._orig_sentinels
        _disable()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestUnavailable(unittest.TestCase):
    def setUp(self):
        _disable()

    def test_accessors_report_disabled(self):
        self.assertFalse(event_logger.is_enabled())
        self.assertIsNone(event_logger.get_client())
        self.assertEqual(event_logger.get_env(), "dev")
        self.assertIsNone(event_logger.get_app_version())

    def test_accessors_report_enabled(self):
        client = FakeFirestoreClient()
        _enable(client, env="prod", app_version="deadbeef")
        try:
            self.assertTrue(event_logger.is_enabled())
            self.assertIs(event_logger.get_client(), client)
            self.assertEqual(event_logger.get_env(), "prod")
            self.assertEqual(event_logger.get_app_version(), "deadbeef")
        finally:
            _disable()

    def test_every_function_raises_library_unavailable(self):
        calls = [
            lambda: lib.commit_prompt_hint(_hint_payload()),
            lambda: lib.get_active_prompt_hints("sheet-A", None),
            lambda: lib.toggle_like_prompt_hint("p1", "user-1"),
            lambda: lib.update_lessons_tested_on_prompt_hint("p1", ["L1"]),
            lambda: lib.visualize_prompt_tree("sheet-A"),
            lambda: lib.trace_prompt("p1"),
            lambda: lib.archive_prompt("p1", "user-1"),
            lambda: lib.archive_all_prompts("sheet-A"),
            lambda: lib.archive_with_admin("p1"),
            lambda: lib.get_all_active_prompts("sheet-A"),
            lambda: lib.commit_scratchpad_prompt_hint(_scratchpad_payload()),
            lambda: lib.visualise_scratchpad_prompt_tree("sheet-A"),
        ]
        self.assertEqual(len(calls), 12)
        for fn in calls:
            with self.assertRaises(lib.LibraryUnavailable):
                fn()
            self.assertTrue(issubclass(lib.LibraryUnavailable, RuntimeError))

    def test_invalid_commit_payload_is_value_error_even_when_unavailable(self):
        # Parity with the Mongo version: validation runs before any DB access,
        # so the commit routes still answer 400 rather than "not saved".
        with self.assertRaisesRegex(ValueError, "non empty user messages"):
            lib.commit_prompt_hint(_hint_payload(userMessages=[]))
        with self.assertRaisesRegex(ValueError, "no user-id provided"):
            lib.commit_scratchpad_prompt_hint(_scratchpad_payload(userId=None))


class TestValidation(_Base):
    def test_empty_user_messages(self):
        with self.assertRaisesRegex(ValueError, "non empty user messages"):
            lib.commit_prompt_hint(_hint_payload(userMessages=[]))
        with self.assertRaisesRegex(ValueError, "non empty user messages"):
            lib.commit_scratchpad_prompt_hint(_scratchpad_payload(userMessages=None))

    def test_missing_user_id(self):
        with self.assertRaisesRegex(ValueError, "no user-id provided"):
            lib.commit_prompt_hint(_hint_payload(userId=""))

    def test_lesson_level_requires_lesson_name(self):
        with self.assertRaisesRegex(ValueError, "lesson name not specified"):
            lib.commit_prompt_hint(_hint_payload(isTextbookLevel=False, lessonName=None))

    def test_textbook_level_cannot_have_lesson_name(self):
        with self.assertRaisesRegex(ValueError, "cannot have lesson name"):
            lib.commit_scratchpad_prompt_hint(
                _scratchpad_payload(isTextbookLevel=True, lessonName="Lesson 1")
            )

    def test_missing_is_textbook_level_defaults_true(self):
        payload = _hint_payload()
        del payload["isTextbookLevel"]
        out = lib.commit_prompt_hint(payload)
        self.assertTrue(out["isTextbookLevel"])

    def test_validation_failure_writes_nothing(self):
        with self.assertRaises(ValueError):
            lib.commit_prompt_hint(_hint_payload(userId=None))
        self.assertEqual(self.client.hints().docs, {})
        self.assertEqual(self.client.committed_batches, [])


class TestCommitScratchpad(_Base):
    def test_writes_doc_with_frontend_id_and_shape(self):
        out = lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-root"))

        docs = self.client.scratchpads().docs
        self.assertEqual(set(docs), {"sp-root"})
        stored = docs["sp-root"]
        self.assertEqual(set(stored), EXPECTED_STORED_SCRATCHPAD_FIELDS)
        self.assertEqual(stored["scratchpadId"], "sp-root")
        self.assertEqual(stored["status"], "sandbox")
        self.assertIsNone(stored["parentScratchpadId"])
        self.assertEqual(stored["childrenScratchpadIds"], [])
        self.assertEqual(stored["committedPromptIds"], [])
        self.assertIsInstance(stored["dateCreated"], dt.datetime)
        # userMessages kept as sent (only HintMessage keys), times not parsed.
        self.assertEqual(
            stored["userMessages"][0],
            {
                "content": "Write 3 questions",
                "hidden": True,
                "role": "user",
                "disabled": False,
                "id": "message_abc12",
                "time_created": "2026-10-12T00:00:00.000Z",
                "time_last_updated": "2026-10-12T00:00:01.000Z",
            },
        )
        for key in ("userPromptHash", "userPromptText", "generationRunId"):
            self.assertIsNone(stored[key])
        # Server-stamped, even though the request carried nothing.
        self.assertEqual(stored["environment"], "dev")
        self.assertEqual(stored["appVersion"], SERVER_APP_VERSION)
        self.assertFalse(stored["parentMissing"])

        self.assertEqual(set(out), EXPECTED_SCRATCHPAD_KEYS)
        self.assertEqual(out["scratchpadId"], "sp-root")
        self.assertEqual(out["status"], "sandbox")
        self.assertIsInstance(out["dateCreated"], str)
        dt.datetime.fromisoformat(out["dateCreated"])  # ISO 8601

    def test_links_parent(self):
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-root"))
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="sp-child", parentScratchpadId="sp-root")
        )
        docs = self.client.scratchpads().docs
        self.assertEqual(docs["sp-root"]["childrenScratchpadIds"], ["sp-child"])
        self.assertEqual(docs["sp-child"]["parentScratchpadId"], "sp-root")
        # Both writes in the same batch.
        self.assertEqual(len(self.client.committed_batches[-1].ops), 2)

    def test_missing_parent_saves_with_parent_missing_flag(self):
        # Pre-migration scratchpads are not in Firestore; the save must still land.
        with self.assertLogs(lib.logger, level="WARNING") as logs:
            out = lib.commit_scratchpad_prompt_hint(
                _scratchpad_payload(scratchpadId="sp-x", parentScratchpadId="nope")
            )
        self.assertEqual(len(logs.output), 1)
        self.assertIn("nope", logs.output[0])

        stored = self.client.scratchpads().docs["sp-x"]
        self.assertIsNone(stored["parentScratchpadId"])
        self.assertTrue(stored["parentMissing"])
        self.assertEqual(set(stored), EXPECTED_STORED_SCRATCHPAD_FIELDS)
        # Normal 201 response shape.
        self.assertEqual(set(out), EXPECTED_SCRATCHPAD_KEYS)
        self.assertEqual(out["scratchpadId"], "sp-x")
        self.assertEqual(out["status"], "sandbox")
        # Only the new doc was written (no dangling parent update).
        self.assertEqual([op for op, _, _ in self.client.committed_batches[-1].ops], ["set"])

    def test_parent_on_other_sheet_saves_without_link(self):
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="sp-other", spreadsheetId="sheet-B")
        )
        with self.assertLogs(lib.logger, level="WARNING") as logs:
            out = lib.commit_scratchpad_prompt_hint(
                _scratchpad_payload(scratchpadId="sp-y", parentScratchpadId="sp-other")
            )
        self.assertEqual(len(logs.output), 1)
        self.assertIn("sp-other", logs.output[0])

        docs = self.client.scratchpads().docs
        self.assertIsNone(docs["sp-y"]["parentScratchpadId"])
        self.assertTrue(docs["sp-y"]["parentMissing"])
        self.assertEqual(docs["sp-other"]["childrenScratchpadIds"], [])
        self.assertEqual(set(out), EXPECTED_SCRATCHPAD_KEYS)

    def test_present_parent_is_not_flagged(self):
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-root"))
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="sp-child", parentScratchpadId="sp-root")
        )
        self.assertFalse(self.client.scratchpads().docs["sp-child"]["parentMissing"])

    def test_metadata_persisted_when_sent(self):
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(
                scratchpadId="sp-m",
                userPromptHash="h" * 64,
                userPromptText="Write 3 questions",
                generationRunId="run-1",
            )
        )
        stored = self.client.scratchpads().docs["sp-m"]
        self.assertEqual(stored["generationRunId"], "run-1")
        self.assertEqual(stored["userPromptHash"], "h" * 64)
        self.assertEqual(stored["userPromptText"], "Write 3 questions")

    def test_environment_and_app_version_cannot_be_overridden_by_client(self):
        _enable(self.client, env="prod", app_version="srv9999")
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(
                scratchpadId="sp-spoof", environment="attacker", appVersion="fake0000"
            )
        )
        # Doc is written under PromptHive/prod/... and stamped with that env.
        self.assertNotIn("sp-spoof", self.client.scratchpads("dev").docs)
        stored = self.client.scratchpads("prod").docs["sp-spoof"]
        self.assertEqual(stored["environment"], "prod")
        self.assertEqual(stored["appVersion"], "srv9999")

    def test_app_version_unknown_is_stored_as_null(self):
        _enable(self.client, env="dev", app_version=None)
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="sp-nover", appVersion="fake0000")
        )
        stored = self.client.scratchpads().docs["sp-nover"]
        self.assertEqual(stored["environment"], "dev")
        self.assertIsNone(stored["appVersion"])

    def test_missing_scratchpad_id_gets_auto_id(self):
        payload = _scratchpad_payload()
        del payload["scratchpadId"]
        out = lib.commit_scratchpad_prompt_hint(payload)
        self.assertTrue(out["scratchpadId"].startswith("auto_"))
        self.assertIn(out["scratchpadId"], self.client.scratchpads().docs)


class TestCommitPromptHint(_Base):
    def test_returns_exact_json_shape_and_stores_fields(self):
        out = lib.commit_prompt_hint(_hint_payload())

        self.assertEqual(set(out), EXPECTED_PROMPT_HINT_KEYS)
        self.assertEqual(out["id"], "auto_1")
        self.assertEqual(out["userId"], "user-1")
        self.assertIsNone(out["lessonName"])
        self.assertEqual(out["spreadsheetId"], "sheet-A")
        self.assertEqual(out["status"], "active")
        self.assertTrue(out["isTextbookLevel"])
        self.assertEqual(out["likes"], 0)
        self.assertIsInstance(out["likes"], int)
        self.assertEqual(out["lessonsTested"], ["Lesson 1"])
        self.assertIsInstance(out["dateCreated"], str)
        dt.datetime.fromisoformat(out["dateCreated"])
        self.assertEqual(out["scratchpadId"], "")  # Mongo returned "" when unlinked
        self.assertEqual(out["temperature"], 0.7)
        self.assertEqual(out["model"], "gpt-4o")
        self.assertEqual(out["userMessages"][0]["content"], "Write 3 questions")
        self.assertNotIn("extraneous", out["userMessages"][0])

        stored = self.client.hints().docs["auto_1"]
        self.assertEqual(set(stored), EXPECTED_STORED_HINT_FIELDS)
        self.assertEqual(stored["likedUsers"], [])
        self.assertEqual(stored["childrenIds"], [])
        self.assertIsNone(stored["parentId"])
        self.assertIsNone(stored["scratchpadId"])
        self.assertIsInstance(stored["dateCreated"], dt.datetime)
        # Stray frontend keys are not persisted.
        for key in ("likes", "timestamp", "systemMessages"):
            self.assertNotIn(key, stored)

    def test_archives_previous_active_for_same_sheet_and_lesson(self):
        first = lib.commit_prompt_hint(_hint_payload())
        # Different lesson / sheet / user must NOT be archived.
        other_lesson = lib.commit_prompt_hint(
            _hint_payload(isTextbookLevel=False, lessonName="Lesson 2")
        )
        other_sheet = lib.commit_prompt_hint(_hint_payload(spreadsheetId="sheet-B"))
        other_user = lib.commit_prompt_hint(_hint_payload(userId="user-2"))

        second = lib.commit_prompt_hint(_hint_payload())

        docs = self.client.hints().docs
        self.assertEqual(docs[first["id"]]["status"], "archive")
        self.assertEqual(docs[second["id"]]["status"], "active")
        self.assertEqual(docs[other_lesson["id"]]["status"], "active")
        self.assertEqual(docs[other_sheet["id"]]["status"], "active")
        self.assertEqual(docs[other_user["id"]]["status"], "active")

        # Archive + set happened in one batch.
        last = self.client.committed_batches[-1]
        self.assertEqual([op for op, _, _ in last.ops], ["update", "set"])

    def test_links_parent_and_scratchpad(self):
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-1"))
        parent = lib.commit_prompt_hint(_hint_payload(userId="user-9"))

        child = lib.commit_prompt_hint(
            _hint_payload(parentId=parent["id"], parentScratchpadId="sp-1")
        )

        hints = self.client.hints().docs
        self.assertEqual(hints[parent["id"]]["childrenIds"], [child["id"]])
        self.assertEqual(hints[child["id"]]["parentId"], parent["id"])
        self.assertEqual(hints[child["id"]]["scratchpadId"], "sp-1")
        self.assertEqual(child["scratchpadId"], "sp-1")
        self.assertEqual(
            self.client.scratchpads().docs["sp-1"]["committedPromptIds"], [child["id"]]
        )
        last = self.client.committed_batches[-1]
        self.assertEqual([op for op, _, _ in last.ops], ["update", "update", "set"])

    def test_missing_parent_prompt_saves_with_parent_missing_flag(self):
        with self.assertLogs(lib.logger, level="WARNING") as logs:
            out = lib.commit_prompt_hint(_hint_payload(parentId="ghost-parent"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ghost-parent", logs.output[0])

        stored = self.client.hints().docs[out["id"]]
        self.assertIsNone(stored["parentId"])
        self.assertTrue(stored["parentMissing"])
        self.assertEqual(stored["status"], "active")
        self.assertEqual(set(stored), EXPECTED_STORED_HINT_FIELDS)
        # Normal 201 response shape.
        self.assertEqual(set(out), EXPECTED_PROMPT_HINT_KEYS)
        self.assertEqual(out["status"], "active")
        self.assertEqual([op for op, _, _ in self.client.committed_batches[-1].ops], ["set"])

    def test_missing_scratchpad_saves_with_parent_missing_flag(self):
        with self.assertLogs(lib.logger, level="WARNING") as logs:
            out = lib.commit_prompt_hint(_hint_payload(parentScratchpadId="ghost-sp"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ghost-sp", logs.output[0])

        stored = self.client.hints().docs[out["id"]]
        self.assertIsNone(stored["scratchpadId"])
        self.assertTrue(stored["parentMissing"])
        self.assertEqual(set(out), EXPECTED_PROMPT_HINT_KEYS)
        self.assertEqual(out["scratchpadId"], "")  # unlinked, same as Mongo
        self.assertNotIn("ghost-sp", self.client.scratchpads().docs)

    def test_missing_parent_still_archives_previous_and_links_present_scratchpad(self):
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-1"))
        first = lib.commit_prompt_hint(_hint_payload())

        with self.assertLogs(lib.logger, level="WARNING"):
            second = lib.commit_prompt_hint(
                _hint_payload(parentId="ghost", parentScratchpadId="sp-1")
            )

        hints = self.client.hints().docs
        self.assertEqual(hints[first["id"]]["status"], "archive")
        self.assertIsNone(hints[second["id"]]["parentId"])
        self.assertEqual(hints[second["id"]]["scratchpadId"], "sp-1")
        self.assertTrue(hints[second["id"]]["parentMissing"])
        self.assertEqual(
            self.client.scratchpads().docs["sp-1"]["committedPromptIds"], [second["id"]]
        )
        last = self.client.committed_batches[-1]
        self.assertEqual([op for op, _, _ in last.ops], ["update", "update", "set"])

    def test_present_parents_are_not_flagged(self):
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-1"))
        parent = lib.commit_prompt_hint(_hint_payload(userId="user-9"))
        child = lib.commit_prompt_hint(
            _hint_payload(parentId=parent["id"], parentScratchpadId="sp-1")
        )
        self.assertFalse(self.client.hints().docs[child["id"]]["parentMissing"])
        self.assertFalse(self.client.hints().docs[parent["id"]]["parentMissing"])

    def test_nullish_parent_ids_are_ignored(self):
        for nullish in (None, "", "null", "undefined", "None"):
            out = lib.commit_prompt_hint(
                _hint_payload(userId=f"u-{nullish}", parentId=nullish, parentScratchpadId=nullish)
            )
            stored = self.client.hints().docs[out["id"]]
            self.assertIsNone(stored["parentId"])
            self.assertFalse(stored["parentMissing"])  # no link requested, not "missing"

    def test_environment_and_app_version_are_server_stamped(self):
        _enable(self.client, env="prod", app_version="srv9999")
        out = lib.commit_prompt_hint(
            _hint_payload(environment="attacker", appVersion="fake0000", generationRunId="run-7")
        )
        stored = self.client.hints("prod").docs[out["id"]]
        self.assertEqual(stored["environment"], "prod")
        self.assertEqual(stored["appVersion"], "srv9999")
        self.assertEqual(stored["generationRunId"], "run-7")  # request-carried stays
        # Default fixture (no metadata in the request) is stamped too.
        _enable(self.client, env="dev")
        out2 = lib.commit_prompt_hint(_hint_payload(userId="u-2"))
        stored2 = self.client.hints().docs[out2["id"]]
        self.assertEqual(stored2["environment"], "dev")
        self.assertEqual(stored2["appVersion"], SERVER_APP_VERSION)
        self.assertIsNone(stored2["generationRunId"])

    def test_every_firestore_call_uses_timeout_10(self):
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-1"))
        parent = lib.commit_prompt_hint(_hint_payload())
        lib.commit_prompt_hint(_hint_payload(parentId=parent["id"], parentScratchpadId="sp-1"))
        lib.toggle_like_prompt_hint(parent["id"], "user-1")
        lib.archive_all_prompts("sheet-A")
        self.assertTrue(self.client.calls)
        for name, _target, kwargs in self.client.calls:
            self.assertEqual(kwargs.get("timeout"), 10, msg=f"{name} missing timeout=10")


class TestUserName(_Base):
    def test_stored_on_both_doc_types_but_not_in_response(self):
        sp_out = lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="sp-u", userName="urban-wallaby")
        )
        hint_out = lib.commit_prompt_hint(_hint_payload(userName="urban-wallaby"))

        self.assertEqual(self.client.scratchpads().docs["sp-u"]["userName"], "urban-wallaby")
        self.assertEqual(self.client.hints().docs[hint_out["id"]]["userName"], "urban-wallaby")
        # Response shapes are unchanged for the frontend.
        self.assertNotIn("userName", sp_out)
        self.assertNotIn("userName", hint_out)
        self.assertEqual(set(sp_out), EXPECTED_SCRATCHPAD_KEYS)
        self.assertEqual(set(hint_out), EXPECTED_PROMPT_HINT_KEYS)

    def test_absent_or_empty_user_name_is_stored_as_null(self):
        a = lib.commit_prompt_hint(_hint_payload(userId="u1"))  # no userName key
        b = lib.commit_prompt_hint(_hint_payload(userId="u2", userName=""))
        c = lib.commit_prompt_hint(_hint_payload(userId="u3", userName=None))
        for out in (a, b, c):
            self.assertIsNone(self.client.hints().docs[out["id"]]["userName"])
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="sp-n"))
        self.assertIsNone(self.client.scratchpads().docs["sp-n"]["userName"])

    def test_user_name_not_echoed_by_reads_or_trees(self):
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="sp-r", userName="urban-wallaby")
        )
        p = lib.commit_prompt_hint(_hint_payload(userName="urban-wallaby"))
        for item in lib.get_active_prompt_hints("sheet-A", None):
            self.assertNotIn("userName", item)
        for item in lib.trace_prompt(p["id"]):
            self.assertNotIn("userName", item)
        for node in lib.visualise_scratchpad_prompt_tree("sheet-A"):
            self.assertNotIn("userName", node["data"])


class TestQueries(_Base):
    def test_get_active_filters_by_sheet_status_and_null_lesson(self):
        textbook = lib.commit_prompt_hint(_hint_payload(userId="u1"))
        lesson = lib.commit_prompt_hint(
            _hint_payload(userId="u2", isTextbookLevel=False, lessonName="Lesson 1")
        )
        lib.commit_prompt_hint(_hint_payload(userId="u3", spreadsheetId="sheet-B"))
        archived = lib.commit_prompt_hint(_hint_payload(userId="u4"))
        lib.archive_prompt(archived["id"], "u4")

        textbook_level = lib.get_active_prompt_hints("sheet-A", None)
        self.assertEqual([p["id"] for p in textbook_level], [textbook["id"]])
        self.assertEqual(set(textbook_level[0]), EXPECTED_PROMPT_HINT_KEYS)
        self.assertIsInstance(textbook_level[0]["dateCreated"], str)

        # "" is treated like None (Mongo behaviour).
        self.assertEqual([p["id"] for p in lib.get_active_prompt_hints("sheet-A", "")],
                         [textbook["id"]])

        lesson_level = lib.get_active_prompt_hints("sheet-A", "Lesson 1")
        self.assertEqual([p["id"] for p in lesson_level], [lesson["id"]])

        # Equality filters only (no order_by); lessonName null is an explicit filter.
        stream_calls = [c for c in self.client.calls if c[0] == "stream"]
        self.assertEqual(
            stream_calls[-1][2]["filters"],
            [("spreadsheetId", "sheet-A"), ("status", "active"), ("lessonName", "Lesson 1")],
        )

    def test_get_active_sorted_by_date_created(self):
        a = lib.commit_prompt_hint(_hint_payload(userId="u1"))
        b = lib.commit_prompt_hint(_hint_payload(userId="u2"))
        c = lib.commit_prompt_hint(_hint_payload(userId="u3"))
        ids = [p["id"] for p in lib.get_active_prompt_hints("sheet-A", None)]
        self.assertEqual(ids, [a["id"], b["id"], c["id"]])

    def test_get_all_active_prompts(self):
        a = lib.commit_prompt_hint(_hint_payload(userId="u1"))
        b = lib.commit_prompt_hint(
            _hint_payload(userId="u2", isTextbookLevel=False, lessonName="Lesson 1")
        )
        lib.commit_prompt_hint(_hint_payload(userId="u3", spreadsheetId="sheet-B"))
        lib.archive_with_admin(a["id"])
        ids = [p["id"] for p in lib.get_all_active_prompts("sheet-A")]
        self.assertEqual(ids, [b["id"]])

    def test_field_filter_path_is_used_when_available(self):
        original = lib.FieldFilter
        lib.FieldFilter = FakeFieldFilter
        try:
            lib.commit_prompt_hint(_hint_payload(userId="u1"))
            self.assertEqual(len(lib.get_active_prompt_hints("sheet-A", None)), 1)
        finally:
            lib.FieldFilter = original


class TestLikesAndLessons(_Base):
    def test_toggle_like_adds_then_removes(self):
        p = lib.commit_prompt_hint(_hint_payload())
        doc = self.client.hints().docs[p["id"]]

        lib.toggle_like_prompt_hint(p["id"], "liker-1")
        self.assertEqual(doc["likedUsers"], ["liker-1"])
        lib.toggle_like_prompt_hint(p["id"], "liker-2")
        self.assertEqual(doc["likedUsers"], ["liker-1", "liker-2"])
        self.assertEqual(lib.get_active_prompt_hints("sheet-A", None)[0]["likes"], 2)

        lib.toggle_like_prompt_hint(p["id"], "liker-1")
        self.assertEqual(doc["likedUsers"], ["liker-2"])
        self.assertEqual(lib.get_active_prompt_hints("sheet-A", None)[0]["likes"], 1)

    def test_toggle_like_missing_prompt(self):
        with self.assertRaisesRegex(ValueError, "prompt not found"):
            lib.toggle_like_prompt_hint("ghost", "liker-1")

    def test_update_lessons_tested_is_add_to_set(self):
        p = lib.commit_prompt_hint(_hint_payload(lessonsTested=["Lesson 1"]))
        lib.update_lessons_tested_on_prompt_hint(p["id"], ["Lesson 1", "Lesson 2"])
        self.assertEqual(
            self.client.hints().docs[p["id"]]["lessonsTested"], ["Lesson 1", "Lesson 2"]
        )
        with self.assertRaises(ValueError):
            lib.update_lessons_tested_on_prompt_hint("ghost", ["x"])


class TestArchive(_Base):
    def test_archive_prompt_checks_owner(self):
        p = lib.commit_prompt_hint(_hint_payload(userId="owner"))
        with self.assertRaisesRegex(Exception, "does not belong to user"):
            lib.archive_prompt(p["id"], "intruder")
        self.assertEqual(self.client.hints().docs[p["id"]]["status"], "active")

        out = lib.archive_prompt(p["id"], "owner")
        self.assertEqual(out, {"message": "successfully archived prompt"})
        self.assertEqual(self.client.hints().docs[p["id"]]["status"], "archive")

    def test_archive_with_admin(self):
        p = lib.commit_prompt_hint(_hint_payload())
        lib.archive_with_admin(p["id"])
        self.assertEqual(self.client.hints().docs[p["id"]]["status"], "archive_admin")
        with self.assertRaises(ValueError):
            lib.archive_with_admin("ghost")

    def test_archive_all_batches_of_at_most_400(self):
        hints = self.client.hints()
        for i in range(850):
            hints.docs[f"p{i}"] = {"spreadsheetId": "sheet-A", "status": "active"}
        for i in range(5):
            hints.docs[f"other{i}"] = {"spreadsheetId": "sheet-B", "status": "active"}

        count = lib.archive_all_prompts("sheet-A")

        self.assertEqual(count, 850)
        sizes = [len(b.ops) for b in self.client.committed_batches]
        self.assertEqual(sizes, [400, 400, 50])
        self.assertTrue(all(n <= 400 for n in sizes))
        self.assertTrue(
            all(hints.docs[f"p{i}"]["status"] == "archive_admin" for i in range(850))
        )
        self.assertTrue(
            all(hints.docs[f"other{i}"]["status"] == "active" for i in range(5))
        )

    def test_archive_all_with_no_matches_commits_nothing(self):
        self.assertEqual(lib.archive_all_prompts("empty-sheet"), 0)
        self.assertEqual(self.client.committed_batches, [])


class TestTrees(_Base):
    def test_scratchpad_tree_shape(self):
        lib.commit_scratchpad_prompt_hint(_scratchpad_payload(scratchpadId="root"))
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="kid-a", parentScratchpadId="root")
        )
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="kid-b", parentScratchpadId="root")
        )
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="grandkid", parentScratchpadId="kid-a")
        )
        lib.commit_scratchpad_prompt_hint(
            _scratchpad_payload(scratchpadId="elsewhere", spreadsheetId="sheet-B")
        )

        forest = lib.visualise_scratchpad_prompt_tree("sheet-A")

        self.assertEqual(len(forest), 1)
        root = forest[0]
        self.assertEqual(set(root), {"data", "children"})
        self.assertEqual(
            set(root["data"]), {"id", "userMessages", "userId", "dateCreated", "lessonName"}
        )
        self.assertEqual(root["data"]["id"], "root")
        self.assertIsInstance(root["data"]["dateCreated"], str)
        self.assertEqual([c["data"]["id"] for c in root["children"]], ["kid-a", "kid-b"])
        self.assertEqual(
            [g["data"]["id"] for g in root["children"][0]["children"]], ["grandkid"]
        )
        self.assertEqual(root["children"][1]["children"], [])

    def test_prompt_tree_shape(self):
        root = lib.commit_prompt_hint(_hint_payload(userId="u1"))
        kid = lib.commit_prompt_hint(_hint_payload(userId="u2", parentId=root["id"]))
        lib.commit_prompt_hint(_hint_payload(userId="u3", spreadsheetId="sheet-B"))

        forest = lib.visualize_prompt_tree("sheet-A")
        self.assertEqual(len(forest), 1)
        self.assertEqual(forest[0]["data"]["id"], root["id"])
        self.assertEqual([c["data"]["id"] for c in forest[0]["children"]], [kid["id"]])

    def test_trace_prompt_root_to_leaf(self):
        root = lib.commit_prompt_hint(_hint_payload(userId="u1"))
        mid = lib.commit_prompt_hint(_hint_payload(userId="u2", parentId=root["id"]))
        leaf = lib.commit_prompt_hint(_hint_payload(userId="u3", parentId=mid["id"]))

        trace = lib.trace_prompt(leaf["id"])
        self.assertEqual([p["id"] for p in trace], [root["id"], mid["id"], leaf["id"]])
        self.assertEqual(set(trace[0]), EXPECTED_PROMPT_HINT_KEYS)

        with self.assertRaisesRegex(ValueError, "prompt not found"):
            lib.trace_prompt("ghost")


if __name__ == "__main__":
    unittest.main()
