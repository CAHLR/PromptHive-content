"""A service that handles sending API requests to ChatGPT and receiving the response.
The only issue is how we get the messages, but that's presumably done as well.
Task
"""

import os
from typing import Literal
from dotenv import load_dotenv
import re

from openai import OpenAI
from openai.types import ResponseFormatJSONObject, ResponseFormatText
from pydantic import BaseModel

from abscribe_backend.models.oatutor_hints import OatutorHints, OatutorQuestions
from abscribe_backend.services.question_difficulty.difficulty_service import (
    auto_calibrate_and_filter_questions,
    difficulty_meta_for_api,
    filter_questions_by_difficulty_range,
    resolve_difficulty_filter,
)

load_dotenv()

DEFAULT_GPT_MODEL = "gpt-4o"
DEFAULT_CHAT_TYPE = "text"
DEFAULT_TEMPERATURE = 0.0
MAX_TOKENS = 1000

# Vision filter for the model picker.
#
# OpenAI's GET /v1/models response has no capability/modality fields (only id,
# created, object, owned_by, shutdown_date), so we cannot query vision support
# from the API. We keep an EXCLUSION list of models whose per-model docs page
# lists "Image Not supported" under Modalities:
#   https://developers.openai.com/api/docs/models/<model-id>
# Sourced from those modality docs (audited against the live catalog). Prefer
# exclusion over an allowlist so newly shipped vision models appear without
# code changes; recheck this list when new model *families* appear (especially
# non-chat / voice / mini text-only lines).
NON_VISION_MODEL_IDS = frozenset(
    {
        # Original GPT-4 text snapshots (not gpt-4o / gpt-4-turbo / gpt-4.1)
        "gpt-4",
        "gpt-4-0314",
        "gpt-4-0613",
        "gpt-4-32k",
        "gpt-4-32k-0314",
        "gpt-4-32k-0613",
    }
)
NON_VISION_MODEL_PREFIXES = (
    "gpt-3.5-turbo",  # Image Not supported
    "gpt-live",  # GPT-Live voice models; Image Not supported (e.g. gpt-live-1)
    "o1-mini",  # Image Not supported
    "o3-mini",  # Image Not supported
)
# Drop non-chat / specialty IDs from the picker (not about vision).
NON_CHAT_MODEL_SUBSTRINGS = (
    "embedding",
    "whisper",
    "tts",
    "transcribe",
    "realtime",
    "audio",
    "dall-e",
    "moderation",
    "davinci",
    "babbage",
    "search",
    "deep-research",
    "computer-use",
    "image-",
)
# Rasch difficulty is on the logit scale (from run_rasch.R / item_params.csv).
DEFAULT_MIN_DIFFICULTY_LOGIT = float(os.getenv("DIFFICULTY_MIN_LOGIT", "-3.0"))
DEFAULT_MAX_DIFFICULTY_LOGIT = float(os.getenv("DIFFICULTY_MAX_LOGIT", "3.0"))

# Set to true/1/yes to filter using precomputed QUESTION_DIFFICULTY_*_CSV instead of
# running generate_responses + Rasch on each request (faster for development).
USE_PRECALIBRATED_DIFFICULTY_CSV = os.getenv(
    "QUESTION_DIFFICULTY_USE_PRECALIBRATED_CSV", ""
).lower() in ("1", "true", "yes")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ALLOWED_CHAT_TYPES = set(["text", "json_object"])


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes")


def apply_difficulty_filter_to_questions(
    questions: list,
    difficulty_filter: dict | None,
    progress_callback=None,
) -> tuple[list, dict]:
    """Feature 1: calibrate + filter generated OATutor rows (one Rasch run)."""
    bounds = resolve_difficulty_filter(
        difficulty_filter,
        default_min_logit=DEFAULT_MIN_DIFFICULTY_LOGIT,
        default_max_logit=DEFAULT_MAX_DIFFICULTY_LOGIT,
    )
    min_logit = bounds["min_logit"]
    max_logit = bounds["max_logit"]
    max_count = bounds["max_count"]
    include_unscored = bounds["include_unscored"]
    n_respondents = bounds["n_respondents"]
    score_saturated_via_anchors = _truthy(
        (difficulty_filter or {}).get("scoreSaturatedViaAnchors")
    ) or _truthy((difficulty_filter or {}).get("includeExisting"))

    # Answer-key check before calibration: the generator often guesses the
    # key, and the respondents are graded against it. Disabled with
    # QUESTION_DIFFICULTY_VERIFY_KEYS=0. Never blocks scoring on failure.
    answer_key_report: dict | None = None
    if os.getenv("QUESTION_DIFFICULTY_VERIFY_KEYS", "1").strip() != "0":
        try:
            from abscribe_backend.services.question_difficulty.answer_key_check import (
                verify_answer_keys,
            )

            questions, answer_key_report = verify_answer_keys(
                questions, progress_callback=progress_callback
            )
        except Exception as e:
            print(f"answer key check failed; continuing without it: {e}")
            answer_key_report = {"error": str(e)}

    if USE_PRECALIBRATED_DIFFICULTY_CSV:
        filtered_questions, difficulty_debug, difficulty_meta = (
            filter_questions_by_difficulty_range(
                questions=questions,
                min_logit=min_logit,
                max_logit=max_logit,
                max_count=max_count,
                include_unscored=include_unscored,
            )
        )
    else:
        filtered_questions, difficulty_debug, difficulty_meta = (
            auto_calibrate_and_filter_questions(
                questions=questions,
                min_logit=min_logit,
                max_logit=max_logit,
                max_count=max_count,
                include_unscored=include_unscored,
                score_saturated_via_anchors=score_saturated_via_anchors,
                n_respondents=n_respondents,
                progress_callback=progress_callback,
            )
        )
    meta = difficulty_meta_for_api(
        {
            **difficulty_meta,
            **{k: v for k, v in bounds.items() if k not in difficulty_meta},
            "totalBefore": len(questions),
            "totalAfter": len(filtered_questions),
            "evaluations": difficulty_debug,
            "calibrationStatus": difficulty_meta.get("calibrationStatus"),
            **(
                {
                    "answerKeyCheck": answer_key_report,
                    # Per-row outcomes keyed by input index, so the logger
                    # (which sees the request's question list) can attach
                    # them to item results.
                    "answerKeyCheckItems": _answer_key_check_items(questions),
                }
                if answer_key_report is not None
                else {}
            ),
        }
    )
    return filtered_questions, meta


def _answer_key_check_items(questions: list) -> list[dict]:
    items: list[dict] = []
    for idx, q in enumerate(questions or []):
        if not isinstance(q, dict) or not q.get("answerKeyCheck"):
            continue
        item = {"index": idx, "answerKeyCheck": q.get("answerKeyCheck")}
        for field in ("answerKeyOriginal", "answerKeySolves", "answerKeyCheckReason"):
            if field in q:
                item[field] = q[field]
        if "answerKeyOriginal" in q:
            item["Answer"] = q.get("Answer")  # the corrected key
            # The browser overlays these onto its own copy of the row, and the
            # sheet view displays "Answer Rendered" in preference to "Answer".
            for field in ("Answer Rendered", "Answer Tutoring"):
                if field in q:
                    item[field] = q[field]
        items.append(item)
    return items


def get_chat(
    messages: list[dict[str, str]],
    model: str = DEFAULT_GPT_MODEL,
    type: str = DEFAULT_CHAT_TYPE,
    temperature: float = DEFAULT_TEMPERATURE,
    stream: bool = True,
):
    print(
        f"get_chat(model={model}, type={type}, temperature={temperature}, stream={stream})"
    )
    # from ..app import app  # Purely for logging.

    """Send a request to the ChatGPT API for a chat completion, given the messages as a list.
    Message objects should be formatted as: {role: "my_role", content: "some content"}."""
    # app.logger.info(f"GPT receives: {messages} as messages.")

    if type not in ALLOWED_CHAT_TYPES:
        type = "text"

    return client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": type},
        stream=stream,
        temperature=temperature,
    )


class DefaultSchema(BaseModel):
    # This schema is likely not what you need; it's just a placeholder for when no other schema is available.
    # Define your actual schema here and register it with a name in the response_types dictionary.
    name: str
    email: str

    def to_json(self) -> dict:
        return {"name": self.name, "email": self.email}


response_types = {
    "default": DefaultSchema,
    "oatutor_hints": OatutorHints,
    "oatutor_questions": OatutorQuestions,
}


def is_non_vision_model(model_id: str) -> bool:
    """True for known text-only / pre-vision chat model IDs."""
    mid = str(model_id or "").strip()
    if not mid:
        return True
    low = mid.lower()
    if low in NON_VISION_MODEL_IDS:
        return True
    for prefix in NON_VISION_MODEL_PREFIXES:
        if low == prefix or low.startswith(f"{prefix}-"):
            return True
    # Original gpt-4 dated snapshots (gpt-4-0314 / gpt-4-0613), not gpt-4o / gpt-4-turbo / gpt-4.1
    if re.match(r"^gpt-4-\d{4}", low):
        return True
    return False


def is_chat_model_candidate(model_id: str) -> bool:
    """Keep general chat model IDs; drop embeddings, audio, search, fine-tunes, etc."""
    mid = str(model_id or "").strip()
    if not mid or mid.startswith("ft:"):
        return False
    low = mid.lower()
    if not low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
        return False
    if any(token in low for token in NON_CHAT_MODEL_SUBSTRINGS):
        return False
    return True


def list_vision_capable_models() -> list[dict]:
    """Live OpenAI model list, filtered to vision-capable chat models."""
    rows = []
    for model in client.models.list().data:
        mid = getattr(model, "id", None) or ""
        if not is_chat_model_candidate(mid):
            continue
        if is_non_vision_model(mid):
            continue
        rows.append(
            {
                "id": mid,
                "created": getattr(model, "created", None),
            }
        )
    rows.sort(key=lambda row: (-(row["created"] or 0), row["id"]))
    if not any(row["id"] == DEFAULT_GPT_MODEL for row in rows):
        rows.insert(0, {"id": DEFAULT_GPT_MODEL, "created": None})
    return rows


def _is_structured_output_unsupported_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "json_schema" in text
        or "structured output" in text
        or "response_format" in text
    ) and (
        "not support" in text
        or "unsupported" in text
        or "invalid parameter" in text
    )


def _parse_structured_completion(model: str, messages, response_schema, temperature: float):
    return (
        client.beta.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=response_schema,
            temperature=temperature,
        )
        .choices[0]
        .message.parsed
    ).to_json()


def get_structured_output(
    messages: list[dict[str, str]],
    response_type: str,
    model: str = DEFAULT_GPT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    isopen: bool = False,
    stream: bool = False,
    difficulty_filter: dict | None = None,
) -> dict:
    requested_model = (model or DEFAULT_GPT_MODEL).strip() or DEFAULT_GPT_MODEL
    used_model = requested_model
    model_fallback = False
    model_fallback_reason = None

    print(
        f"get_structured_output(response_type={response_type}, model={requested_model}, temperature={temperature}, stream={stream}, isopen={isopen})"
    )

    problem_name = None

    # Extract problem name from the system message, without modifying what gets sent to the API
    for msg in messages:
        if msg.get("role") == "system" and msg.get("content", "").startswith("The problem is as follows"):
            match = re.search(r"Problem Name:\s*(\S+)", msg.get("content", ""))
            if match:
                problem_name = match.group(1).strip()
            break

    def with_model_meta(payload: dict) -> dict:
        payload["model"] = used_model
        payload["modelRequested"] = requested_model
        payload["modelFallback"] = model_fallback
        if model_fallback_reason:
            payload["modelFallbackReason"] = model_fallback_reason
        payload["problemName"] = problem_name
        return payload

    if isopen:
        # If open-ended, use normal chat completion with the full message context
        response = client.chat.completions.create(
            model=used_model,
            messages=messages,
            temperature=temperature,
        )

        return with_model_meta(
            {
                "response": response.choices[0].message.content,
            }
        )

    # Get structured response — pass the chosen model through; fall back to gpt-4o
    # only if OpenAI rejects it for structured output, and tell the client.
    response_schema = response_types.get(response_type, DefaultSchema)

    try:
        structured_response = _parse_structured_completion(
            used_model, messages, response_schema, temperature
        )
    except Exception as exc:
        if (
            used_model != DEFAULT_GPT_MODEL
            and _is_structured_output_unsupported_error(exc)
        ):
            model_fallback = True
            model_fallback_reason = str(exc)
            used_model = DEFAULT_GPT_MODEL
            print(
                f"structured output unsupported for {requested_model}; "
                f"falling back to {used_model}: {exc}"
            )
            structured_response = _parse_structured_completion(
                used_model, messages, response_schema, temperature
            )
        else:
            raise

    if response_type == "oatutor_questions":
        try:
            questions = structured_response.get("questions", [])
            skip_calibration = _truthy(
                (difficulty_filter or {}).get("skipCalibration")
            )
            if skip_calibration:
                structured_response["difficultyFilter"] = {
                    "skippedCalibration": True,
                    "totalBefore": len(questions),
                    "totalAfter": len(questions),
                }
            else:
                filtered_questions, difficulty_meta = (
                    apply_difficulty_filter_to_questions(
                        questions, difficulty_filter
                    )
                )
                structured_response["questions"] = filtered_questions
                structured_response["difficultyFilter"] = difficulty_meta
        except Exception as e:
            # Fail-open to avoid blocking question generation.
            structured_response["difficultyFilterError"] = str(e)

    return with_model_meta(structured_response)
