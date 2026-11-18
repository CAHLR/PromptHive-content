"""Last-line ``FINAL ANSWER: <answer>`` extraction.

Shared by the answer-key checker (solver replies) and, when
``QUESTION_DIFFICULTY_RESPONDENT_REASONING=1``, the simulated respondents.
Kept in its own module so ``generate_responses`` can import it without the
``answer_key_check`` -> ``difficulty_service`` -> ``generate_responses`` cycle.
"""

from __future__ import annotations

import re

from abscribe_backend.services.question_difficulty.io import clean_text

FINAL_ANSWER_RE = re.compile(
    r"^\s*\**\s*FINAL ANSWER\s*:\s*\**\s*(.*?)\s*\**\s*$", re.IGNORECASE
)


def parse_final_answer(text: str) -> str | None:
    """Return the cleaned answer from the last ``FINAL ANSWER:`` line, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    for line in reversed(text.splitlines()):
        m = FINAL_ANSWER_RE.match(line)
        if m:
            cleaned = clean_text(m.group(1))
            return cleaned if cleaned else None
    return None
