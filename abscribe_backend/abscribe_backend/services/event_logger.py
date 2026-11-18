"""Firestore event logger for PromptHive.

Design (per lab review, 2026-10-08):
- One collection per event type under ``PromptHive/{env}/`` in the lab
  Firebase project. ``env`` comes from ``PROMPTHIVE_FIRESTORE_ENV`` (default
  ``dev``). Service-account path comes from ``FIREBASE_SERVICE_ACCOUNT_PATH``
  (or ``GOOGLE_APPLICATION_CREDENTIALS`` as fallback). If neither is set the
  module warns once and logging stays off — the app runs normally.
- Writes happen on a background thread with a 10 s per-call timeout so the
  Flask response is never blocked or failed by logging. Every exception
  inside the write thread is swallowed and logged at WARNING.
- Nothing in this module raises to callers. ``log_event`` and helpers always
  return quietly.

Public surface:
    event_logger.init()
    event_logger.log_event(collection, data)       # low level
    event_logger.log_question_generation(...)      # /chatGPT/json
    event_logger.log_difficulty_estimation(...)    # calibrate / score

The module exposes a small amount of mutable state (``_client``, ``_enabled``)
so tests can inject a fake Firestore client without touching the real SDK.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state (mutable; tests may override _client / _enabled / _executor).
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_initialized: bool = False
_enabled: bool = False
_env: str = "dev"
_app_version: Optional[str] = None
_client: Any = None  # Firestore client (or a fake, in tests)
_executor: Optional[ThreadPoolExecutor] = None
_gevent_init_done: bool = False  # process-wide guard for grpc gevent init

# Firebase/Firestore sentinel for server-side timestamps. Resolved lazily so
# tests can run without firebase_admin installed.
_server_timestamp_sentinel: Any = None

ROOT_COLLECTION = "PromptHive"
QUESTION_GENERATIONS_COLLECTION = "questionGenerations"
DIFFICULTY_ESTIMATES_COLLECTION = "difficultyEstimates"

WRITE_TIMEOUT_SECONDS = 10.0
PROMPT_TEXT_LIMIT = 20_000  # characters
ITEM_TEXT_LIMIT = 1_000     # characters


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
def _resolve_app_version() -> Optional[str]:
    """Resolve the app version string stamped on every Firestore event.

    Priority order (per lab review, 2026-10-11):
      1. ``PROMPTHIVE_APP_VERSION`` environment variable (deploy-time override).
      2. ``git rev-parse --short HEAD`` run at the repo root.
      3. First line of ``.deployed-backend-commit`` in the ``PromptHive``
         folder one level above ``abscribe_backend`` (populated by the deploy
         script on hosts that are not git repos, e.g. Quadro).

    Returns ``None`` if every source is empty or unreadable so the field in
    Firestore is ``null`` rather than a misleading placeholder.
    """
    env_version = (os.environ.get("PROMPTHIVE_APP_VERSION") or "").strip()
    if env_version:
        return env_version

    # abscribe_backend/abscribe_backend/services/event_logger.py
    #   parents[0] -> services/
    #   parents[1] -> abscribe_backend/ (inner package)
    #   parents[2] -> abscribe_backend/ (repo root for the backend)
    #   parents[3] -> PromptHive/ (sibling of abscribe_backend/)
    try:
        repo_root = Path(__file__).resolve().parents[2]
    except Exception:
        repo_root = None

    if repo_root is not None:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(repo_root),
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            ).strip()
            if out:
                return out
        except Exception:
            pass

    try:
        prompthive_root = Path(__file__).resolve().parents[3]
    except Exception:
        return None
    commit_file = prompthive_root / ".deployed-backend-commit"
    try:
        if commit_file.is_file():
            with commit_file.open("r", encoding="utf-8", errors="replace") as fp:
                first_line = fp.readline().strip()
            if first_line:
                return first_line
    except Exception:
        pass
    return None


def _maybe_init_grpc_gevent() -> None:
    """Enable gRPC's gevent integration when the process is monkey-patched.

    Firebase Admin's Firestore client talks gRPC under the hood. When the
    process is running under a gevent monkey-patched event loop (as our
    gunicorn ``-k gevent`` workers do) gRPC blocks the greenlet unless we
    call ``grpc.experimental.gevent.init_gevent()`` once at startup. This
    helper calls it exactly once per process, and only when
    ``gevent.monkey.is_module_patched("socket")`` is true, so plain
    non-gevent runs (tests, scripts) are completely untouched.

    Safety guarantees:
      * Runs at most once per process (``_gevent_init_done`` guard), even
        if ``init()`` is invoked repeatedly.
      * Never raises. A missing ``gevent`` or ``grpc`` import, or any
        other error, is swallowed and logged at INFO so the app continues.
      * Logs exactly one INFO line describing whether the init ran or was
        skipped and why.
    """
    global _gevent_init_done
    if _gevent_init_done:
        return
    # Flip the flag before doing any work so a transient failure does not
    # cause us to retry (and re-log) on every init() call.
    _gevent_init_done = True
    try:
        from gevent import monkey  # type: ignore
    except Exception as exc:
        logger.info(
            "event_logger: gevent not importable; grpc gevent init skipped (%s).",
            exc,
        )
        return
    try:
        patched = bool(monkey.is_module_patched("socket"))
    except Exception as exc:
        logger.info(
            "event_logger: could not read gevent patch status; "
            "grpc gevent init skipped (%s).",
            exc,
        )
        return
    if not patched:
        logger.info(
            "event_logger: gevent monkey-patch not active; "
            "grpc gevent init skipped."
        )
        return
    try:
        import grpc.experimental.gevent as grpc_gevent  # type: ignore

        grpc_gevent.init_gevent()
    except Exception as exc:
        logger.info(
            "event_logger: grpc.experimental.gevent.init_gevent() "
            "failed, continuing without it (%s).",
            exc,
        )
        return
    logger.info(
        "event_logger: grpc.experimental.gevent.init_gevent() ran "
        "(gunicorn gevent worker detected)."
    )


def init() -> None:
    """Initialise Firestore client if credentials are configured.

    Idempotent. Safe to call multiple times. Never raises.
    """
    global _initialized, _enabled, _env, _app_version, _client
    global _executor, _server_timestamp_sentinel

    with _lock:
        if _initialized:
            return
        _initialized = True

        _env = (os.environ.get("PROMPTHIVE_FIRESTORE_ENV") or "dev").strip() or "dev"
        _app_version = _resolve_app_version()

        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="event-logger"
            )

        cred_path = (
            os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or ""
        ).strip()

        if not cred_path:
            logger.warning(
                "event_logger: Firestore logging disabled "
                "(FIREBASE_SERVICE_ACCOUNT_PATH / GOOGLE_APPLICATION_CREDENTIALS "
                "not set). The app will run normally without event logging."
            )
            _enabled = False
            return

        # Must run before any firebase_admin / grpc import so the gevent
        # integration is in place for every subsequent Firestore call.
        _maybe_init_grpc_gevent()

        try:
            import firebase_admin  # type: ignore
            from firebase_admin import credentials, firestore  # type: ignore

            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(cred_path))
            _client = firestore.client()
            _server_timestamp_sentinel = firestore.SERVER_TIMESTAMP
            _enabled = True
            logger.info(
                "event_logger: Firestore logging enabled "
                "(env=%s, appVersion=%s).",
                _env,
                _app_version,
            )
        except Exception as exc:  # pragma: no cover - exercised in integration
            logger.warning(
                "event_logger: initialisation failed, logging disabled: %s", exc
            )
            _enabled = False
            _client = None


# ---------------------------------------------------------------------------
# Public accessors (shared with other Firestore-backed services)
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    """True when ``init()`` succeeded and a Firestore client is available."""
    return bool(_enabled) and _client is not None


def get_client() -> Any:
    """Return the shared Firestore client, or ``None`` when logging is disabled."""
    return _client if _enabled else None


def get_env() -> str:
    """Return the PromptHive environment segment (e.g. ``"dev"``, ``"prod"``)."""
    return _env


def get_app_version() -> Optional[str]:
    """Return the app version resolved by ``init()`` (``None`` if unknown)."""
    return _app_version


# ---------------------------------------------------------------------------
# Low-level write
# ---------------------------------------------------------------------------
def _write_document(collection: str, payload: dict) -> None:
    """Perform the actual Firestore write. Runs on a background thread."""
    if _client is None:
        return

    try:
        doc_ref = (
            _client.collection(ROOT_COLLECTION)
            .document(_env)
            .collection(collection)
            .document()
        )
    except Exception as exc:
        logger.warning(
            "event_logger: could not build doc ref for %s: %s", collection, exc
        )
        return

    try:
        try:
            doc_ref.set(payload, timeout=WRITE_TIMEOUT_SECONDS)
        except TypeError:
            # Older google-cloud-firestore clients (and test fakes) may not
            # accept a `timeout` kwarg on `set`. Fall back to a plain set.
            doc_ref.set(payload)
    except Exception as exc:
        logger.warning(
            "event_logger: write to %s failed: %s", collection, exc
        )


def log_event(collection: str, payload: dict) -> Optional[Future]:
    """Fire-and-forget Firestore write.

    Returns the submitted ``Future`` (or ``None`` if logging is disabled) so
    that unit tests can wait on completion. Production callers do not need
    to touch the return value.
    """
    if not _enabled or _client is None or _executor is None:
        return None
    try:
        return _executor.submit(_write_document, collection, dict(payload))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("event_logger: executor submit failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _joined_messages(messages: Optional[Iterable[Any]]) -> str:
    if messages is None:
        return ""
    parts: list[str] = []
    try:
        for msg in messages:
            if isinstance(msg, dict):
                role = str(msg.get("role", ""))
                content = msg.get("content", "")
                if isinstance(content, (list, dict)):
                    content = str(content)
                parts.append(f"[{role}] {content}")
            else:
                parts.append(str(msg))
    except Exception:
        parts.append(str(messages))
    return "\n".join(parts)


def _prompt_text(messages: Optional[Iterable[Any]]) -> str:
    return _joined_messages(messages)[:PROMPT_TEXT_LIMIT]


def _prompt_hash(messages: Optional[Iterable[Any]]) -> str:
    joined = _joined_messages(messages)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()


def _sha256_text(text: Optional[str]) -> Optional[str]:
    """Return the SHA-256 of ``text`` (or ``None`` if empty/not a string)."""
    if text is None:
        return None
    try:
        if not isinstance(text, str):
            text = str(text)
    except Exception:
        return None
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _questions_hash(questions: Optional[Iterable[Any]]) -> str:
    parts: list[str] = []
    if questions:
        for q in questions:
            if isinstance(q, dict):
                parts.append(
                    str(q.get("itemId") or q.get("Problem Name") or "")
                )
                parts.append(_pick_question_text(q) or "")
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def _pick_question_text(q: Any) -> Optional[str]:
    if not isinstance(q, dict):
        return None
    for key in ("Body Text", "body_text", "text", "stem", "content"):
        v = q.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _pick_answer(q: Any) -> Optional[str]:
    """Correct-answer key for an OATutor question dict."""
    if not isinstance(q, dict):
        return None
    for key in ("Answer", "answer_key", "answer"):
        v = q.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            if v:
                return v
        else:
            try:
                return str(v)
            except Exception:
                return None
    return None


def _pick_answer_type(q: Any) -> Optional[str]:
    """``answerType`` for an OATutor question dict (``numeric`` / ``algebra`` / ``mc``)."""
    if not isinstance(q, dict):
        return None
    for key in ("answerType", "answer_type"):
        v = q.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _extract_user_text_from_messages(messages: Optional[Iterable[Any]]) -> str:
    """Fallback for ``userPromptText``: join every user-role message whose
    content is a plain string, in order. Image parts of vision messages
    (``{type: "image_url", ...}``) are skipped so the derived text matches
    what the user actually wrote.
    """
    if not messages:
        return ""
    parts: list[str] = []
    try:
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role", "")) != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                stripped = content.strip()
                if stripped:
                    parts.append(stripped)
                continue
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                    # image_url parts intentionally skipped.
    except Exception:
        return ""
    return "\n".join(parts)


def _truncate(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return None
    return value[:limit]


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _default_calibration_temperature() -> float:
    """Mirror ``DEFAULT_CALIBRATION_TEMPERATURE`` from the respondent module.

    Imported lazily so this module still loads in isolated unit tests that
    stub out the ``abscribe_backend`` package. If the import fails (e.g.
    because ``abscribe_backend.services.question_difficulty`` has not been
    initialised) we fall back to the literal 1.75 kept in sync with
    ``services/question_difficulty/respondents.py``.
    """
    try:
        from abscribe_backend.services.question_difficulty.respondents import (
            DEFAULT_CALIBRATION_TEMPERATURE,
        )

        return float(DEFAULT_CALIBRATION_TEMPERATURE)
    except Exception:
        return 1.75  # keep in sync with respondents.DEFAULT_CALIBRATION_TEMPERATURE


def _calibration_env_defaults() -> dict:
    """Read calibration model / temperature / token cap from env (never fails).

    Temperature falls back to the respondent module's
    ``DEFAULT_CALIBRATION_TEMPERATURE`` so a log entry matches the value the
    live calibration actually ran at.
    """
    model = (os.environ.get("QUESTION_DIFFICULTY_CALIBRATION_MODEL") or "").strip()
    if not model:
        model = "gpt-4o"
    default_temp = _default_calibration_temperature()
    raw_temp = (os.environ.get("QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE") or "").strip()
    try:
        temperature = float(raw_temp) if raw_temp else default_temp
    except (TypeError, ValueError):
        temperature = default_temp
    try:
        token_cap = int(os.environ.get("QUESTION_DIFFICULTY_MAX_TOKENS") or "150")
    except (TypeError, ValueError):
        token_cap = 150
    return {"model": model, "temperature": temperature, "token_cap": token_cap}


def _stamp_metadata(
    event_type: str,
    context: Optional[dict],
    duration_ms: float,
    error: Optional[str],
    prompt_hash: str,
) -> dict:
    context = context or {}
    # Two user fields on every event (per lab review, 2026-10-11):
    #   * ``userId``   = the raw local-storage identifier the prompt library
    #                    already saves under ``hintviewer_user_id`` (so
    #                    Firestore records join cleanly to prompt-library rows).
    #   * ``userName`` = the display handle the top bar already shows
    #                    (``generateNameFromSeed(userId)`` — e.g. "urban-wallaby")
    #                    for human-friendly dashboards.
    return {
        "eventType": event_type,
        "environment": _env,
        "appVersion": _app_version,
        "userId": context.get("userId"),
        "userName": context.get("userName"),
        "spreadsheetId": context.get("spreadsheetId"),
        "lessonName": context.get("lessonName"),
        "promptId": context.get("promptId"),
        "promptHash": prompt_hash,
        "generationRunId": context.get("generationRunId"),
        # Scratchpad node this run belongs to (PromptHive/{env}/scratchpadPromptHints
        # doc id). Lets a prompt node and its records be joined directly.
        "scratchpadId": context.get("scratchpadId"),
        "clientTimestamp": context.get("clientTimestamp"),
        "serverTimestamp": _server_timestamp_sentinel,
        "durationMs": duration_ms,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Public helpers for the two event types
# ---------------------------------------------------------------------------
def log_question_generation(
    *,
    messages: Optional[Iterable[Any]],
    requested_model: Optional[str],
    used_model: Optional[str],
    model_fallback: bool,
    model_fallback_reason: Optional[str],
    temperature: Optional[float],
    difficulty_filter: Optional[dict],
    questions: Optional[list],
    context: Optional[dict],
    duration_ms: float,
    error: Optional[str] = None,
) -> Optional[Future]:
    """Log one call to /chatGPT/json for ``oatutor_questions``."""
    difficulty_filter = difficulty_filter or {}
    context = context or {}

    questions = questions or []
    question_log: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        question_log.append(
            {
                "id": q.get("id") or q.get("itemId"),
                "problemName": q.get("problemName") or q.get("Problem Name"),
                "text": _truncate(_pick_question_text(q), ITEM_TEXT_LIMIT),
                # Answer key for human review in Firestore. Pulled from the
                # OATutor "Answer" column (fallbacks: ``answer_key`` /
                # ``answer``). Truncated to ``ITEM_TEXT_LIMIT`` so a stray
                # long free-text answer cannot blow up the document size.
                "answer": _truncate(_pick_answer(q), ITEM_TEXT_LIMIT),
                "answerType": _pick_answer_type(q),
            }
        )

    data = _stamp_metadata(
        "question_generation",
        context,
        duration_ms,
        error,
        prompt_hash=_prompt_hash(messages),
    )

    # ``userPromptText`` and ``userPromptHash`` give a stable per-prompt key for
    # grouping records in Firestore. Priority order:
    #   1. ``context["userPromptText"]`` from the client (preferred).
    #   2. Backend fallback: join every user-role plain-text message. This
    #      covers cases where the client could not compute the text (e.g.
    #      older frontend, non-JS client) so Firestore still has a stable
    #      per-prompt key.
    # The hash prefers the client-supplied value and falls back to a fresh
    # SHA-256 of whichever text we end up using.
    user_prompt_text_raw = context.get("userPromptText")
    user_prompt_text: Optional[str] = None
    if isinstance(user_prompt_text_raw, str) and user_prompt_text_raw:
        user_prompt_text = user_prompt_text_raw[:PROMPT_TEXT_LIMIT]
    else:
        derived = _extract_user_text_from_messages(messages)
        if derived:
            user_prompt_text_raw = derived
            user_prompt_text = derived[:PROMPT_TEXT_LIMIT]
    user_prompt_hash = context.get("userPromptHash")
    if not isinstance(user_prompt_hash, str) or not user_prompt_hash:
        user_prompt_hash = _sha256_text(user_prompt_text_raw)

    data.update(
        {
            "promptText": _prompt_text(messages),
            "userPromptText": user_prompt_text,
            "userPromptHash": user_prompt_hash,
            "modelRequested": requested_model,
            "modelUsed": used_model,
            "modelFallback": bool(model_fallback),
            "modelFallbackReason": model_fallback_reason,
            "temperature": temperature,
            "questionsRequested": _coerce_int(
                difficulty_filter.get("questionsToGenerate")
            ),
            "useOriginalItemsAsTemplates": context.get(
                "useOriginalItemsAsTemplates"
            ),
            "useGeneratedItemsAsTemplates": context.get(
                "useGeneratedItemsAsTemplates"
            ),
            "batchNumber": _coerce_int(context.get("batchNumber")),
            "skipCalibration": bool(difficulty_filter.get("skipCalibration")),
            "questions": question_log,
            "questionCount": len(question_log),
        }
    )
    return log_event(QUESTION_GENERATIONS_COLLECTION, data)


ANSWER_KEY_CHECK_ITEM_FIELDS = (
    "answerKeyCheck",
    "answerKeyOriginal",
    "answerKeySolves",
    "answerKeyCheckReason",
)


def _answer_key_check_by_index(calibration_meta: Optional[dict]) -> dict[int, dict]:
    """Index -> per-row answer-key-check fields from ``meta["answerKeyCheckItems"]``.

    The calibrate route logs the request's question list, which predates the
    key check, so the service also ships the per-row outcomes in the meta.
    """
    out: dict[int, dict] = {}
    items = (calibration_meta or {}).get("answerKeyCheckItems")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if isinstance(idx, int):
            out[idx] = item
    return out


def _build_item_results(
    questions: Optional[list],
    scores: Optional[list],
    key_check_by_index: Optional[dict[int, dict]] = None,
) -> list[dict]:
    """Pair every per-item score with the matching question's text/id/answer.

    Each item row carries ``answer`` + ``answerType`` from the OATutor
    question row at ``score["index"]`` (when the index resolves) so the
    Firestore record is self-contained — reviewers can read a difficulty log
    without cross-referencing a spreadsheet. When the answer-key check ran,
    ``answerKeyCheck`` / ``answerKeyOriginal`` / ``answerKeySolves`` are
    included (from the question row, else from ``key_check_by_index``).
    """
    questions = questions or []
    scores = scores or []
    key_check_by_index = key_check_by_index or {}
    results: list[dict] = []
    for score in scores:
        if not isinstance(score, dict):
            continue
        text: Optional[str] = None
        answer: Optional[str] = None
        answer_type: Optional[str] = None
        key_check: dict = {}
        idx = score.get("index")
        if isinstance(idx, int) and 0 <= idx < len(questions):
            q_row = questions[idx]
            text = _pick_question_text(q_row)
            answer = _pick_answer(q_row)
            answer_type = _pick_answer_type(q_row)
            source = q_row if isinstance(q_row, dict) and q_row.get("answerKeyCheck") else None
            if source is None:
                source = key_check_by_index.get(idx)
            if isinstance(source, dict):
                for field in ANSWER_KEY_CHECK_ITEM_FIELDS:
                    if field in source and source[field] is not None:
                        key_check[field] = source[field]
                if "answerKeyOriginal" in key_check and source is not q_row:
                    # Row predates the correction: report the corrected key.
                    corrected = _pick_answer(source)
                    if corrected is not None:
                        answer = corrected
        scored = score.get("scored")
        if scored is None:
            scored = score.get("difficultyLogit") is not None
        row = {
            "id": score.get("itemId") or score.get("id"),
            "problemName": score.get("problemName"),
            "text": _truncate(text, ITEM_TEXT_LIMIT),
            "answer": _truncate(answer, ITEM_TEXT_LIMIT),
            "answerType": answer_type,
            "logit": score.get("difficultyLogit"),
            "band": score.get("difficultyLabel"),
            "scored": bool(scored),
            "unscoredReason": None if scored else score.get("reason"),
        }
        if key_check:
            if "answerKeyOriginal" in key_check:
                key_check["answerKeyOriginal"] = _truncate(
                    str(key_check["answerKeyOriginal"]), ITEM_TEXT_LIMIT
                )
            row.update(key_check)
        results.append(row)
    return results


def log_difficulty_estimation(
    *,
    source: str,  # "estimate_button" | "after_generation"
    questions: Optional[list],
    scores: Optional[list],
    n_respondents: Optional[int],
    calibration_meta: Optional[dict],
    context: Optional[dict],
    duration_ms: float,
    error: Optional[str] = None,
) -> Optional[Future]:
    """Log one call to score-existing-questions or calibrate-generated-questions."""
    calibration_meta = calibration_meta or {}
    context = context or {}

    item_results = _build_item_results(
        questions, scores, _answer_key_check_by_index(calibration_meta)
    )
    items_total = len(questions or []) or len(item_results)
    items_unscored = sum(1 for r in item_results if not r.get("scored"))
    items_scored_ids = [r["id"] for r in item_results if r.get("scored")]

    # Cell counts are the response-matrix dimensions (``respondents x calibrated
    # items``), not the number of OATutor rows. The calibration service puts
    # them in the returned meta when the Rasch run actually built a response
    # matrix; if they are missing (early-return, error path, or endpoints that
    # do not run live calibration) we fall back to item counts and tell the
    # consumer by naming the fields ``itemsTotal`` / ``itemsUnscored`` instead
    # of ``cellsTotal`` / ``cellsMissingCount``.
    cells_total = _coerce_int(calibration_meta.get("cellsTotal"))
    cells_missing = _coerce_int(calibration_meta.get("cellsMissingCount"))
    have_cell_matrix = cells_total is not None and cells_missing is not None

    env_defaults = _calibration_env_defaults()
    respondent_temperature = calibration_meta.get("respondentTemperature")
    if respondent_temperature is None:
        respondent_temperature = calibration_meta.get("calibrationTemperature")
    if respondent_temperature is None:
        respondent_temperature = env_defaults["temperature"]

    data = _stamp_metadata(
        "difficulty_estimation",
        context,
        duration_ms,
        error,
        prompt_hash=_questions_hash(questions),
    )
    user_prompt_hash = context.get("userPromptHash")
    if not isinstance(user_prompt_hash, str) or not user_prompt_hash:
        user_prompt_hash = None
    data.update(
        {
            "source": source,
            "nRespondents": _coerce_int(n_respondents),
            "model": env_defaults["model"],
            "temperature": respondent_temperature,
            "tokenCap": env_defaults["token_cap"],
            "userPromptHash": user_prompt_hash,
            "itemsScored": items_scored_ids,
            "itemResults": item_results,
            "calibrationStatus": calibration_meta.get("calibrationStatus"),
            "respondentAbility": calibration_meta.get("respondentAbility"),
        }
    )
    if have_cell_matrix:
        data["cellsTotal"] = cells_total
        data["cellsMissingCount"] = cells_missing
    else:
        data["itemsTotal"] = items_total
        data["itemsUnscored"] = items_unscored
    answer_key_check = calibration_meta.get("answerKeyCheck")
    if isinstance(answer_key_check, dict):
        data["answerKeyCheck"] = dict(answer_key_check)
    return log_event(DIFFICULTY_ESTIMATES_COLLECTION, data)


# ---------------------------------------------------------------------------
# Test helpers (not for production code)
# ---------------------------------------------------------------------------
def _reset_for_tests() -> None:
    """Clear module state. Only used by the unit tests."""
    global _initialized, _enabled, _client, _env, _app_version
    global _executor, _server_timestamp_sentinel, _gevent_init_done
    with _lock:
        _initialized = False
        _enabled = False
        _client = None
        _env = "dev"
        _app_version = None
        _server_timestamp_sentinel = None
        _gevent_init_done = False
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
