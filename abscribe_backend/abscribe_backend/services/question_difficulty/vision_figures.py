"""Fetch and attach figure URLs for multimodal (vision) respondent prompts.

Primary source: OATutor sheet ``Images`` column (space-delimited absolute HTTPS URLs).
Mirrors OATutor AgentChatbox vision MIME allow-list and data-URL validation.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

VISION_IMAGE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
    }
)

VISION_SAFE_DATA_URL_RE = re.compile(
    r"^data:image/(png|jpe?g|gif|webp);base64,",
    re.IGNORECASE,
)

# Extension fallback when Content-Type is missing/wrong.
_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

DEFAULT_FETCH_TIMEOUT_SEC = 15.0
FIGURE_CONTEXT_TEXT = (
    "This figure is part of the problem — use it to answer."
)

# Legacy skip reason (pre-regression-fix). Figure failures no longer skip items;
# they calibrate text-only and set figureUnavailable on the evaluation instead.
SKIP_REASON_IMAGE_UNAVAILABLE = "image_unavailable"


def is_vision_safe_image_data_url(data_url: str) -> bool:
    return isinstance(data_url, str) and bool(VISION_SAFE_DATA_URL_RE.match(data_url))


def parse_image_urls(raw: Any) -> list[str]:
    """Split Images-column / image_urls field into absolute http(s) URLs."""
    if raw is None or (isinstance(raw, float) and str(raw) == "nan"):
        return []
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return []
    urls: list[str] = []
    for token in text.split():
        token = token.strip()
        if token.lower().startswith(("http://", "https://")):
            urls.append(token)
    # Preserve order, drop duplicates
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _normalize_mime(content_type: str | None, url: str) -> str | None:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in VISION_IMAGE_MIME_TYPES:
        if mime == "image/jpg":
            return "image/jpeg"
        return mime
    # Some CDNs omit type; infer from extension.
    lower = url.lower().split("?", 1)[0]
    for ext, mapped in _EXT_TO_MIME.items():
        if lower.endswith(ext):
            return mapped
    return None


class FigureCache:
    """Per-calibration-run cache: URL -> data URL (or None on failure)."""

    def __init__(self, *, timeout_sec: float = DEFAULT_FETCH_TIMEOUT_SEC) -> None:
        self.timeout_sec = timeout_sec
        self._store: dict[str, str | None] = {}
        self._errors: dict[str, str] = {}

    def get(self, url: str) -> str | None:
        data_url, _err = self.get_result(url)
        return data_url

    def get_result(self, url: str) -> tuple[str | None, str | None]:
        """Return (data_url, None) on success or (None, error_message) on failure."""
        if url in self._store:
            data = self._store[url]
            if data:
                return data, None
            return None, self._errors.get(url) or "fetch failed"
        data_url, err = self._fetch(url)
        self._store[url] = data_url
        if err:
            self._errors[url] = err
        return data_url, err

    def fetch_many(self, urls: list[str]) -> list[str]:
        attached: list[str] = []
        for url in urls:
            data_url = self.get(url)
            if data_url:
                attached.append(data_url)
        return attached

    def fetch_many_with_errors(
        self, urls: list[str]
    ) -> tuple[list[str], list[str]]:
        attached: list[str] = []
        errors: list[str] = []
        for url in urls:
            data_url, err = self.get_result(url)
            if data_url:
                attached.append(data_url)
            elif err:
                errors.append(f"{url}: {err}")
        return attached, errors

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in self._store.items() if v}

    def load_dict(self, mapping: dict[str, str]) -> None:
        for url, data_url in (mapping or {}).items():
            if is_vision_safe_image_data_url(data_url):
                self._store[url] = data_url
                self._errors.pop(url, None)

    def _fetch(self, url: str) -> tuple[str | None, str | None]:
        try:
            req = Request(
                url,
                headers={"User-Agent": "PromptHive-vision/1.0"},
                method="GET",
            )
            with urlopen(req, timeout=self.timeout_sec) as resp:
                content_type = resp.headers.get("Content-Type")
                body = resp.read()
            mime = _normalize_mime(content_type, url)
            if not mime:
                err = f"unsupported content-type {content_type!r}"
                logger.warning("vision: %s for %s", err, url)
                return None, err
            if not body:
                err = "empty body"
                logger.warning("vision: %s for %s", err, url)
                return None, err
            b64 = base64.b64encode(body).decode("ascii")
            data_url = f"data:{mime};base64,{b64}"
            if not is_vision_safe_image_data_url(data_url):
                err = "data URL failed validation"
                logger.warning("vision: %s for %s", err, url)
                return None, err
            return data_url, None
        except HTTPError as exc:
            err = f"HTTP {exc.code}"
            if getattr(exc, "reason", None):
                err = f"HTTP {exc.code}: {exc.reason}"
            logger.warning("vision: failed to fetch %s: %s", url, err)
            return None, err
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc) or exc)
            if "CERTIFICATE" in reason.upper() or "SSL" in reason.upper():
                err = f"SSL error: {reason}"
            else:
                err = f"URL error: {reason}"
            logger.warning("vision: failed to fetch %s: %s", url, err)
            return None, err
        except TimeoutError:
            err = "timeout"
            logger.warning("vision: failed to fetch %s: %s", url, err)
            return None, err
        except OSError as exc:
            err = f"OS error: {exc}"
            logger.warning("vision: failed to fetch %s: %s", url, err)
            return None, err
        except ValueError as exc:
            err = f"invalid URL/value: {exc}"
            logger.warning("vision: failed to fetch %s: %s", url, err)
            return None, err
        except Exception as exc:  # noqa: BLE001 — never throw during calibration
            err = f"unexpected error: {exc}"
            logger.warning("vision: failed to fetch %s: %s", url, err)
            return None, err


def build_multimodal_user_content(
    text_prompt: str,
    data_urls: list[str],
) -> str | list[dict[str, Any]]:
    """Build OpenAI-style user content; plain string when no figures."""
    safe = [u for u in data_urls if is_vision_safe_image_data_url(u)]
    if not safe:
        return text_prompt
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": f"{FIGURE_CONTEXT_TEXT}\n\n{text_prompt}"},
    ]
    for data_url in safe:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "auto"},
            }
        )
    return parts


def resolve_item_data_urls(item: Any, cache: FigureCache) -> list[str]:
    """Resolve attached data URLs for one item row / dict using the run cache."""
    raw = ""
    if isinstance(item, dict):
        raw = item.get("image_urls", "")
    elif hasattr(item, "get"):
        raw = item.get("image_urls", "")
    elif hasattr(item, "index") and "image_urls" in getattr(item, "index", []):
        raw = item["image_urls"]
    return cache.fetch_many(parse_image_urls(raw))


def figures_summary_line(figure_status_by_item_id: dict[str, dict[str, Any]]) -> str:
    """One-line meta summary, e.g. 'figures: 1 attached, 0 failed'."""
    attached = sum(
        1 for s in figure_status_by_item_id.values() if s.get("figureAttached") is True
    )
    failed = sum(
        1 for s in figure_status_by_item_id.values() if s.get("figureAttached") is False
    )
    return f"figures: {attached} attached, {failed} failed"


def prepare_items_for_vision(
    items_df: Any,
    index_to_item_id: dict[int, str | None],
    item_id_to_question_index: dict[str, int],
    index_to_skip_reason: dict[int, str],
    *,
    cache: FigureCache | None = None,
) -> tuple[
    Any,
    dict[int, str | None],
    dict[int, str],
    FigureCache,
    dict[str, dict[str, Any]],
]:
    """Fetch figures once per run; keep all items for calibration.

    Vision is additive: if a figure cannot be attached, the item stays in
    ``items_df`` with ``image_urls`` cleared (text-only respondents).
    Never clears ``index_to_item_id`` for figure failures.

    Returns ``figure_status_by_item_id`` only for items that had image URLs:
    ``{figureAttached, figureCount, figureError}``.
    """
    cache = cache or FigureCache()
    figure_status_by_item_id: dict[str, dict[str, Any]] = {}
    if items_df is None or len(items_df) == 0:
        return (
            items_df,
            index_to_item_id,
            index_to_skip_reason,
            cache,
            figure_status_by_item_id,
        )

    items_df = items_df.copy()
    if "image_urls" not in items_df.columns:
        items_df["image_urls"] = ""

    for idx, row in items_df.iterrows():
        item_id = str(row["item_id"])
        urls = parse_image_urls(row.get("image_urls", ""))
        if not urls:
            continue
        attached, errors = cache.fetch_many_with_errors(urls)
        if attached:
            figure_status_by_item_id[item_id] = {
                "figureAttached": True,
                "figureCount": len(attached),
                "figureError": None,
            }
            continue
        # Keep calibrating on text — same as pre-vision behavior.
        items_df.at[idx, "image_urls"] = ""
        err_msg = "; ".join(errors) if errors else "fetch failed"
        figure_status_by_item_id[item_id] = {
            "figureAttached": False,
            "figureCount": 0,
            "figureError": err_msg,
        }
        logger.warning(
            "vision: figure unavailable for %s — calibrating text-only (%s)",
            item_id,
            err_msg,
        )

    _ = item_id_to_question_index
    return (
        items_df.reset_index(drop=True),
        index_to_item_id,
        index_to_skip_reason,
        cache,
        figure_status_by_item_id,
    )


def save_figures_cache(path: str | Path, cache: FigureCache) -> None:
    path = Path(path)
    path.write_text(json.dumps(cache.to_dict()), encoding="utf-8")


def load_figures_cache(path: str | Path) -> FigureCache:
    cache = FigureCache()
    path = Path(path)
    if not path.is_file():
        return cache
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cache.load_dict(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("vision: could not load figures cache %s: %s", path, exc)
    return cache
