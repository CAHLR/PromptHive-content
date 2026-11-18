"""Firestore-backed prompt library (PromptHint + ScratchpadPromptHint).

Drop-in replacement for :mod:`prompt_library_service` (the MongoEngine
version, which is left in place). Public function names, signatures,
validation rules and return JSON shapes are kept identical so the frontend
does not change.

Storage layout (shares the client/env with :mod:`event_logger`)::

    PromptHive/{env}/promptHints/{autoId}
    PromptHive/{env}/scratchpadPromptHints/{scratchpadId}

Design notes
------------
* Every function raises :class:`LibraryUnavailable` when no Firestore client
  is configured, so callers can degrade gracefully (the routes in ``app.py``
  already do).
* Every Firestore call passes ``timeout=10``.
* Queries use equality filters only (no ``order_by``) so no composite index
  is required. Results are sorted in Python by ``dateCreated`` where order
  matters.
* Writes that touch more than one document go through a batch.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable, Iterable, Optional

from abscribe_backend.services import event_logger

logger = logging.getLogger(__name__)

try:  # Newer google-cloud-firestore clients prefer FieldFilter.
    from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore
except Exception:  # pragma: no cover - older clients / libs not installed
    FieldFilter = None  # type: ignore[assignment]


ROOT_COLLECTION = "PromptHive"
PROMPT_HINTS_COLLECTION = "promptHints"
SCRATCHPAD_COLLECTION = "scratchpadPromptHints"

TIMEOUT_SECONDS = 10
MAX_BATCH_WRITES = 400

# Status values mirror models/prompt_hint.py and models/prompt_hint_scratchpad.py
STATUS_ACTIVE = "active"
STATUS_ARCHIVE = "archive"
STATUS_ARCHIVE_ADMIN = "archive_admin"
SCRATCHPAD_STATUS_SANDBOX = "sandbox"

HINT_MESSAGE_KEYS = (
    "content",
    "hidden",
    "role",
    "disabled",
    "id",
    "time_created",
    "time_last_updated",
)
_NULLISH_IDS = {"", "none", "null", "undefined"}

# Optional request-level metadata persisted verbatim when present.
# ``environment`` and ``appVersion`` are stamped server-side (see
# ``_metadata_fields``) and can never be set by the client.
_OPTIONAL_METADATA_KEYS = (
    "userPromptHash",
    "userPromptText",
    "generationRunId",
)


class LibraryUnavailable(RuntimeError):
    """Raised when the Firestore client is not configured."""


# ---------------------------------------------------------------------------
# Client / sentinel plumbing
# ---------------------------------------------------------------------------
# Resolved lazily so this module imports cleanly when the Firestore libraries
# are absent (the same posture as event_logger). Tests override these.
_ARRAY_UNION: Optional[Callable[[list], Any]] = None
_SERVER_TIMESTAMP: Any = None


def _require_client() -> tuple[Any, str]:
    client = event_logger.get_client()
    if client is None:
        raise LibraryUnavailable("prompt library unavailable: Firestore is not configured")
    return client, event_logger.get_env()


def _ensure_sentinels() -> None:
    global _ARRAY_UNION, _SERVER_TIMESTAMP
    if _ARRAY_UNION is not None and _SERVER_TIMESTAMP is not None:
        return
    try:
        from google.cloud.firestore_v1 import ArrayUnion, SERVER_TIMESTAMP  # type: ignore
    except Exception:
        try:
            from firebase_admin import firestore as _fa  # type: ignore

            ArrayUnion, SERVER_TIMESTAMP = _fa.ArrayUnion, _fa.SERVER_TIMESTAMP
        except Exception as exc:  # pragma: no cover - libs not installed
            raise LibraryUnavailable(
                "prompt library unavailable: Firestore sentinels not importable"
            ) from exc
    if _ARRAY_UNION is None:
        _ARRAY_UNION = ArrayUnion
    if _SERVER_TIMESTAMP is None:
        _SERVER_TIMESTAMP = SERVER_TIMESTAMP


def _array_union(values: Iterable[Any]) -> Any:
    _ensure_sentinels()
    return _ARRAY_UNION(list(values))  # type: ignore[misc]


def _server_timestamp() -> Any:
    _ensure_sentinels()
    return _SERVER_TIMESTAMP


def _prompt_hints(client: Any, env: str) -> Any:
    return client.collection(ROOT_COLLECTION).document(env).collection(PROMPT_HINTS_COLLECTION)


def _scratchpads(client: Any, env: str) -> Any:
    return client.collection(ROOT_COLLECTION).document(env).collection(SCRATCHPAD_COLLECTION)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------
def _where_eq(query: Any, field: str, value: Any) -> Any:
    """Equality filter that works on old and new Firestore clients."""
    if FieldFilter is not None:
        try:
            return query.where(filter=FieldFilter(field, "==", value))
        except TypeError:
            pass
    return query.where(field, "==", value)


def _stream_where(collection_ref: Any, filters: Iterable[tuple[str, Any]]) -> list:
    query = collection_ref
    for field, value in filters:
        query = _where_eq(query, field, _firestore_safe(value))
    return list(query.stream(timeout=TIMEOUT_SECONDS))


def _get_existing(doc_ref: Any, what: str, doc_id: str) -> Any:
    snapshot = doc_ref.get(timeout=TIMEOUT_SECONDS)
    if not getattr(snapshot, "exists", False):
        raise ValueError(f"{what} not found: {doc_id}")
    return snapshot


def _clean_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if cleaned.lower() in _NULLISH_IDS:
        return None
    return cleaned


def _firestore_safe(value: Any) -> Any:
    """Recursively coerce values into Firestore-storable primitives."""
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    if isinstance(value, dict):
        return {str(k): _firestore_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_firestore_safe(v) for v in value]
    if isinstance(value, _dt.datetime):
        return value
    if hasattr(value, "value") and not callable(value.value):  # Enum-like
        return _firestore_safe(value.value)
    return str(value)


def _iso(value: Any) -> Any:
    """Render dateCreated as an ISO 8601 string (None passes through)."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.isoformat()
    return str(value)


def _sort_key(doc: dict) -> tuple:
    created = doc.get("dateCreated")
    if isinstance(created, _dt.datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=_dt.timezone.utc)
        return (0, created.timestamp())
    # Pending server timestamps / unknown types sort last, stable.
    return (1, 0.0)


def _snapshot_to_dict(snapshot: Any) -> dict:
    data = dict(snapshot.to_dict() or {})
    data["_docId"] = snapshot.id
    return data


# ---------------------------------------------------------------------------
# Validation / normalisation (mirrors the Mongo models)
# ---------------------------------------------------------------------------
def _normalise_messages(messages: Any) -> list[dict]:
    """Keep exactly the HintMessage fields; never parse time strings."""
    if not messages:
        return []
    out: list[dict] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        out.append({key: _firestore_safe(raw.get(key)) for key in HINT_MESSAGE_KEYS})
    return out


def _validate(user_messages: list, user_id: Any, is_textbook_level: bool, lesson_name: Any) -> None:
    if not user_messages:
        raise ValueError("please provide non empty user messages")
    if not user_id:
        raise ValueError("no user-id provided")
    if not is_textbook_level and not lesson_name:
        raise ValueError("lesson name not specified for lesson-level prompt")
    if is_textbook_level and lesson_name:
        raise ValueError("textbook-level prompt cannot have lesson name")


def _common_fields(json: dict) -> dict:
    """Fields shared by prompt hints and scratchpad docs, already validated."""
    user_messages = _normalise_messages(json.get("userMessages"))
    user_id = json.get("userId")
    lesson_name = json.get("lessonName") or None
    is_textbook_level = json.get("isTextbookLevel")
    if is_textbook_level is None:  # MongoEngine applies the field default for None
        is_textbook_level = True
    is_textbook_level = bool(is_textbook_level)
    _validate(user_messages, user_id, is_textbook_level, lesson_name)

    lessons_tested = json.get("lessonsTested")
    user_name = json.get("userName")
    return {
        "userMessages": user_messages,
        "userId": str(user_id),
        # Display handle (generateNameFromSeed(userId)) sent by the frontend;
        # stored for dashboards, deliberately not echoed in the response JSON.
        "userName": str(user_name) if user_name not in (None, "") else None,
        "lessonName": lesson_name,
        "spreadsheetId": _firestore_safe(json.get("spreadsheetId")),
        "isTextbookLevel": is_textbook_level,
        "lessonsTested": [str(x) for x in lessons_tested] if lessons_tested else [],
    }


def _metadata_fields(json: dict) -> dict:
    """Request-carried metadata plus server-stamped environment/appVersion."""
    fields = {key: _firestore_safe(json.get(key)) for key in _OPTIONAL_METADATA_KEYS}
    fields["environment"] = event_logger.get_env()
    fields["appVersion"] = event_logger.get_app_version()
    return fields


def _resolve_parent(doc_ref: Any, what: str, parent_id: str) -> Optional[Any]:
    """Return the parent snapshot, or ``None`` (after a WARNING) if it is missing.

    Prompts committed before the Firestore migration do not exist here, so a
    dangling parent link must not block the save.
    """
    snapshot = doc_ref.get(timeout=TIMEOUT_SECONDS)
    if getattr(snapshot, "exists", False):
        return snapshot
    logger.warning("prompt_library_firestore: %s not found, saving without link: %s", what, parent_id)
    return None


# ---------------------------------------------------------------------------
# Serialisers (same shapes as the Mongo to_json() / visualize())
# ---------------------------------------------------------------------------
def _prompt_hint_to_json(doc: dict, doc_id: Optional[str] = None) -> dict:
    liked = doc.get("likedUsers") or []
    return {
        "id": doc_id or doc.get("_docId"),
        "userMessages": doc.get("userMessages") or [],
        "userId": doc.get("userId"),
        "lessonName": doc.get("lessonName"),
        "spreadsheetId": doc.get("spreadsheetId"),
        "status": doc.get("status"),
        "isTextbookLevel": doc.get("isTextbookLevel"),
        "likes": len(liked),
        "lessonsTested": doc.get("lessonsTested") or [],
        "dateCreated": _iso(doc.get("dateCreated")),
        "scratchpadId": doc.get("scratchpadId") or "",
        "temperature": doc.get("temperature"),
        "model": doc.get("model"),
    }


def _scratchpad_to_json(doc: dict, doc_id: Optional[str] = None) -> dict:
    return {
        "scratchpadId": doc.get("scratchpadId") or doc_id or doc.get("_docId"),
        "userMessages": doc.get("userMessages") or [],
        "userId": doc.get("userId"),
        "lessonName": doc.get("lessonName"),
        "spreadsheetId": doc.get("spreadsheetId"),
        "status": doc.get("status"),
        "isTextbookLevel": doc.get("isTextbookLevel"),
        "lessonsTested": doc.get("lessonsTested") or [],
        "dateCreated": _iso(doc.get("dateCreated")),
    }


def _tree_data(doc: dict, node_id: str) -> dict:
    return {
        "id": node_id,
        "userMessages": doc.get("userMessages") or [],
        "userId": doc.get("userId"),
        "dateCreated": _iso(doc.get("dateCreated")),
        "lessonName": doc.get("lessonName"),
    }


def _build_tree(docs: list[dict], id_of: Callable[[dict], str], parent_key: str) -> list[dict]:
    """Group in-memory docs into {"data": ..., "children": [...]} forests.

    Roots are docs whose ``parent_key`` is null. Siblings are ordered by
    dateCreated (oldest first), matching Mongo's append order.
    """
    ordered = sorted(docs, key=_sort_key)
    children_by_parent: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for doc in ordered:
        parent = _clean_id(doc.get(parent_key))
        if parent is None:
            roots.append(doc)
        else:
            children_by_parent.setdefault(parent, []).append(doc)

    def build(doc: dict, seen: frozenset) -> dict:
        node_id = id_of(doc)
        kids = [] if node_id in seen else children_by_parent.get(node_id, [])
        return {
            "data": _tree_data(doc, node_id),
            "children": [build(child, seen | {node_id}) for child in kids],
        }

    return [build(root, frozenset()) for root in roots]


def _update(doc_ref: Any, fields: dict) -> None:
    doc_ref.update(fields, timeout=TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Public API (same names/signatures as prompt_library_service)
# ---------------------------------------------------------------------------
def commit_prompt_hint(json: dict) -> dict:
    # Validate before touching the client so malformed payloads still 400
    # (ValueError) when the library is unavailable, as with the Mongo version.
    json = json or {}
    fields = _common_fields(json)
    client, env = _require_client()

    hints = _prompt_hints(client, env)
    scratchpads = _scratchpads(client, env)
    new_ref = hints.document()
    new_id = new_ref.id

    parent_id = _clean_id(json.get("parentId"))
    scratchpad_id = _clean_id(json.get("parentScratchpadId"))

    temperature = json.get("temperature")
    try:
        temperature = float(temperature) if temperature is not None else 0.0
    except (TypeError, ValueError):
        temperature = 0.0

    batch = client.batch()

    # Archive the user's previous ACTIVE prompt for the same sheet + lesson.
    previous = _stream_where(
        hints,
        (
            ("userId", fields["userId"]),
            ("lessonName", fields["lessonName"]),
            ("status", STATUS_ACTIVE),
            ("spreadsheetId", fields["spreadsheetId"]),
        ),
    )
    for snap in previous:
        batch.update(snap.reference, {"status": STATUS_ARCHIVE})

    # Missing parents (e.g. prompts from before the Firestore migration) must
    # not block the save: drop the link, flag parentMissing, and carry on.
    parent_missing = False

    if parent_id is not None:
        parent_ref = hints.document(parent_id)
        if _resolve_parent(parent_ref, "parent prompt", parent_id) is not None:
            batch.update(parent_ref, {"childrenIds": _array_union([new_id])})
        else:
            parent_id = None
            parent_missing = True

    if scratchpad_id is not None:
        scratchpad_ref = scratchpads.document(scratchpad_id)
        if _resolve_parent(scratchpad_ref, "scratchpad prompt", scratchpad_id) is not None:
            batch.update(scratchpad_ref, {"committedPromptIds": _array_union([new_id])})
        else:
            scratchpad_id = None
            parent_missing = True

    doc = {
        **fields,
        "status": STATUS_ACTIVE,
        "likedUsers": [],
        "dateCreated": _server_timestamp(),
        "parentId": parent_id,
        "childrenIds": [],
        "parentMissing": parent_missing,
        "scratchpadId": scratchpad_id,
        "temperature": temperature,
        "model": _firestore_safe(json.get("model")),
        **_metadata_fields(json),
    }

    batch.set(new_ref, doc)
    batch.commit(timeout=TIMEOUT_SECONDS)

    # SERVER_TIMESTAMP is resolved server-side; report the commit time locally.
    response_doc = dict(doc)
    response_doc["dateCreated"] = _dt.datetime.now(_dt.timezone.utc)
    return _prompt_hint_to_json(response_doc, doc_id=new_id)


def get_active_prompt_hints(spreadsheet_id: str, lesson_name: Optional[str]) -> list[dict]:
    client, env = _require_client()
    if not lesson_name:
        lesson_name = None
    snaps = _stream_where(
        _prompt_hints(client, env),
        (
            ("spreadsheetId", spreadsheet_id),
            ("status", STATUS_ACTIVE),
            ("lessonName", lesson_name),
        ),
    )
    docs = sorted((_snapshot_to_dict(s) for s in snaps), key=_sort_key)
    return [_prompt_hint_to_json(d) for d in docs]


def toggle_like_prompt_hint(prompt_id: str, user_id: str) -> None:
    client, env = _require_client()
    prompt_id = _clean_id(prompt_id)
    if prompt_id is None:
        raise ValueError("no promptId provided")
    ref = _prompt_hints(client, env).document(prompt_id)
    snap = _get_existing(ref, "prompt", prompt_id)
    liked = list((snap.to_dict() or {}).get("likedUsers") or [])
    if user_id in liked:
        liked.remove(user_id)
    else:
        liked.append(user_id)
    _update(ref, {"likedUsers": liked})


def update_lessons_tested_on_prompt_hint(prompt_id: str, new_lessons_tested: list[str]) -> None:
    client, env = _require_client()
    prompt_id = _clean_id(prompt_id)
    if prompt_id is None:
        raise ValueError("no promptId provided")
    ref = _prompt_hints(client, env).document(prompt_id)
    _get_existing(ref, "prompt", prompt_id)
    lessons = [str(x) for x in (new_lessons_tested or [])]
    if not lessons:
        return
    _update(ref, {"lessonsTested": _array_union(lessons)})


def visualize_prompt_tree(spreadsheet_id: str) -> list[dict]:
    client, env = _require_client()
    snaps = _stream_where(_prompt_hints(client, env), (("spreadsheetId", spreadsheet_id),))
    docs = [_snapshot_to_dict(s) for s in snaps]
    return _build_tree(docs, id_of=lambda d: d["_docId"], parent_key="parentId")


def trace_prompt(prompt_id: str) -> list[dict]:
    client, env = _require_client()
    hints = _prompt_hints(client, env)
    trace: list[dict] = []
    visited: set[str] = set()
    current = _clean_id(prompt_id)
    while current is not None and current not in visited:
        visited.add(current)
        snap = _get_existing(hints.document(current), "prompt", current)
        doc = _snapshot_to_dict(snap)
        trace.append(_prompt_hint_to_json(doc))
        current = _clean_id(doc.get("parentId"))
    return trace[::-1]


def archive_prompt(prompt_id: str, user_id: str) -> dict:
    client, env = _require_client()
    prompt_id = _clean_id(prompt_id)
    if prompt_id is None:
        raise ValueError("no promptId provided")
    ref = _prompt_hints(client, env).document(prompt_id)
    snap = _get_existing(ref, "prompt", prompt_id)
    owner = (snap.to_dict() or {}).get("userId")
    if str(owner) != str(user_id):
        raise Exception("prompt does not belong to user")
    _update(ref, {"status": STATUS_ARCHIVE})
    return {"message": "successfully archived prompt"}


def archive_all_prompts(spreadsheet_id: str) -> int:
    """Archive (admin) every prompt for a spreadsheet. Returns the count."""
    client, env = _require_client()
    snaps = _stream_where(_prompt_hints(client, env), (("spreadsheetId", spreadsheet_id),))
    total = 0
    pending = 0
    batch = client.batch()
    for snap in snaps:
        batch.update(snap.reference, {"status": STATUS_ARCHIVE_ADMIN})
        pending += 1
        total += 1
        if pending >= MAX_BATCH_WRITES:
            batch.commit(timeout=TIMEOUT_SECONDS)
            batch = client.batch()
            pending = 0
    if pending:
        batch.commit(timeout=TIMEOUT_SECONDS)
    return total


def archive_with_admin(prompt_id: str) -> None:
    client, env = _require_client()
    prompt_id = _clean_id(prompt_id)
    if prompt_id is None:
        raise ValueError("no promptId provided")
    ref = _prompt_hints(client, env).document(prompt_id)
    _get_existing(ref, "prompt", prompt_id)
    _update(ref, {"status": STATUS_ARCHIVE_ADMIN})


def get_all_active_prompts(spreadsheet_id: str) -> list[dict]:
    client, env = _require_client()
    snaps = _stream_where(
        _prompt_hints(client, env),
        (("spreadsheetId", spreadsheet_id), ("status", STATUS_ACTIVE)),
    )
    docs = sorted((_snapshot_to_dict(s) for s in snaps), key=_sort_key)
    return [_prompt_hint_to_json(d) for d in docs]


def commit_scratchpad_prompt_hint(json: dict) -> dict:
    json = json or {}
    fields = _common_fields(json)  # validate first (see commit_prompt_hint)
    client, env = _require_client()

    scratchpads = _scratchpads(client, env)
    scratchpad_id = _clean_id(json.get("scratchpadId"))
    if scratchpad_id is None:
        scratchpad_id = scratchpads.document().id
    ref = scratchpads.document(scratchpad_id)

    parent_id = _clean_id(json.get("parentScratchpadId"))

    batch = client.batch()

    # A missing parent (or one on another spreadsheet) must not block the
    # save: drop the link, flag parentMissing, and carry on.
    parent_missing = False
    if parent_id is not None:
        parent_ref = scratchpads.document(parent_id)
        parent_snap = _resolve_parent(parent_ref, "parent scratchpad prompt", parent_id)
        if parent_snap is not None:
            # Mongo looked the parent up by scratchpad_id AND spreadsheet_id.
            parent_sheet = (parent_snap.to_dict() or {}).get("spreadsheetId")
            if str(parent_sheet) != str(fields["spreadsheetId"]):
                logger.warning(
                    "prompt_library_firestore: parent scratchpad prompt belongs to "
                    "another spreadsheet, saving without link: %s",
                    parent_id,
                )
                parent_snap = None
        if parent_snap is not None:
            batch.update(parent_ref, {"childrenScratchpadIds": _array_union([scratchpad_id])})
        else:
            parent_id = None
            parent_missing = True

    doc = {
        **fields,
        "scratchpadId": scratchpad_id,
        "status": SCRATCHPAD_STATUS_SANDBOX,
        "dateCreated": _server_timestamp(),
        "parentScratchpadId": parent_id,
        "childrenScratchpadIds": [],
        "committedPromptIds": [],
        "parentMissing": parent_missing,
        **_metadata_fields(json),
    }

    batch.set(ref, doc)
    batch.commit(timeout=TIMEOUT_SECONDS)

    response_doc = dict(doc)
    response_doc["dateCreated"] = _dt.datetime.now(_dt.timezone.utc)
    return _scratchpad_to_json(response_doc, doc_id=scratchpad_id)


def visualise_scratchpad_prompt_tree(spreadsheet_id: str) -> list[dict]:
    client, env = _require_client()
    snaps = _stream_where(_scratchpads(client, env), (("spreadsheetId", spreadsheet_id),))
    docs = [_snapshot_to_dict(s) for s in snaps]
    return _build_tree(
        docs,
        id_of=lambda d: str(d.get("scratchpadId") or d["_docId"]),
        parent_key="parentScratchpadId",
    )
