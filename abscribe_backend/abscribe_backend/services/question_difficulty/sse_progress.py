"""SSE helpers for difficulty calibrate/score progress (same-request streaming)."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from typing import Any

from flask import Response, request, stream_with_context


_SENTINEL = object()


def client_wants_sse() -> bool:
    """True when the client asks for text/event-stream (UI streaming path)."""
    accept = (request.headers.get("Accept") or "").lower()
    return "text/event-stream" in accept


def progress_message(phase: str, done: int, total: int) -> str:
    if phase == "simulating":
        return f"Simulating respondents {done}/{total}"
    if phase == "computing":
        return "Computing difficulty…"
    if phase == "generating":
        return "Generating questions…"
    if phase == "checking_keys":
        return "Checking answer keys"
    return phase or "Working…"


def make_progress_event(done: int, total: int, phase: str) -> dict[str, Any]:
    return {
        "type": "progress",
        "phase": phase,
        "done": int(done),
        "total": int(total),
        "message": progress_message(phase, int(done), int(total)),
    }


def sse_response_from_worker(
    run: Callable[[Callable[[int, int, str], None] | None], dict[str, Any]],
) -> Response:
    """Run ``run(progress_callback)`` in a thread; stream progress/result/error as SSE.

    Soft failures should be returned from ``run`` as a normal dict (emitted as
    ``type: result``). Unexpected exceptions become ``type: error``.
    """
    events: queue.Queue = queue.Queue()

    def progress_callback(done: int, total: int, phase: str = "simulating") -> None:
        events.put(make_progress_event(done, total, phase))

    def worker() -> None:
        try:
            payload = run(progress_callback)
            events.put({"type": "result", "payload": payload})
        except Exception as exc:
            events.put({"type": "error", "error": str(exc)})
        finally:
            events.put(_SENTINEL)

    thread = threading.Thread(
        target=worker,
        name="difficulty-sse-worker",
        daemon=True,
    )
    thread.start()

    def generate():
        try:
            while True:
                item = events.get()
                if item is _SENTINEL:
                    break
                yield f"data: {json.dumps(item, default=str)}\n\n"
        finally:
            thread.join(timeout=30)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers=headers,
    )
