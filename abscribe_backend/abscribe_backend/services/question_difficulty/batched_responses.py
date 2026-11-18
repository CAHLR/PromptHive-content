"""Batched single-prompt respondent generation (all items per respondent)."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd


def _format_hint(item: dict[str, Any]) -> str:
    answer_type = item["answer_type"]
    if answer_type == "numeric":
        return "Answer with a single number or fraction (e.g. -4/7)."
    if answer_type == "algebra":
        return "Answer with a simplified expression or equation (e.g. y=4x-3)."
    if answer_type == "string":
        return "Answer with a single word or short phrase (e.g. perpendicular)."
    if answer_type == "mc":
        choices = item.get("mc_choices", "")
        return f"Answer with exactly one of: {choices}"
    return "Give the final answer."


def render_batched_prompt(items_df: pd.DataFrame, figure_cache=None) -> str | list:
    """Render all items in one prompt (Q1, Q2, ... format).

    When ``figure_cache`` supplies attached figures, returns multimodal content.
    """
    from .vision_figures import (
        build_multimodal_user_content,
        resolve_item_data_urls,
    )

    lines = [
        "Answer every question below.",
        "Use one line per question in exactly this format:",
        "Q1: <answer>",
        "Q2: <answer>",
        "...",
        "Give only the final answer on each line. No extra prose.",
        "",
    ]
    all_data_urls: list[str] = []
    for idx, (_, item) in enumerate(items_df.iterrows(), start=1):
        lines.append(f"Q{idx}: {item['stem']}")
        lines.append(f"  ({_format_hint(item)})")
        lines.append("")
        if figure_cache is not None:
            all_data_urls.extend(resolve_item_data_urls(item, figure_cache))
    text = "\n".join(lines).strip()
    if not all_data_urls:
        return text
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in all_data_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return build_multimodal_user_content(text, unique)


def parse_batched_response(raw_text: str, n_items: int) -> dict[int, str]:
    """Parse Qn: answer labels from a batched model response.

    Returns a 1-based question index -> answer string mapping.
    Missing labels are omitted (treated as non-response by caller).
    """
    if not raw_text or not isinstance(raw_text, str):
        return {}

    text = raw_text.strip()
    parsed: dict[int, str] = {}

    # Primary: split on Q<number>: markers
    marker = re.compile(r"Q(\d+)\s*[:\.\)]\s*", re.IGNORECASE)
    matches = list(marker.finditer(text))
    if matches:
        for i, match in enumerate(matches):
            qnum = int(match.group(1))
            if qnum < 1 or qnum > n_items:
                continue
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            answer = text[start:end].strip()
            answer = re.sub(r"\s+", " ", answer)
            parsed[qnum] = answer

    # Fallback: line-by-line Qn: answers
    if not parsed:
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r"^Q(\d+)\s*[:\.\)]\s*(.+)$", line, re.IGNORECASE)
            if m:
                qnum = int(m.group(1))
                if 1 <= qnum <= n_items:
                    parsed[qnum] = m.group(2).strip()

    # Fallback: numbered list "1. answer" / "1) answer"
    if not parsed:
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r"^(\d+)\s*[\.\)]\s*(.+)$", line)
            if m:
                qnum = int(m.group(1))
                if 1 <= qnum <= n_items:
                    parsed[qnum] = m.group(2).strip()

    return parsed


def item_index_to_id(items_df: pd.DataFrame) -> dict[int, str]:
    """Map 1-based Q index to item_id."""
    return {
        idx: str(row["item_id"])
        for idx, (_, row) in enumerate(items_df.iterrows(), start=1)
    }
