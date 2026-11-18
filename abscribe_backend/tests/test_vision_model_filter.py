"""Unit tests for vision-capable model filtering (no live OpenAI calls)."""

from abscribe_backend.services.chatgpt_service import (
    is_chat_model_candidate,
    is_non_vision_model,
)


def test_known_non_vision_models_excluded():
    assert is_non_vision_model("gpt-3.5-turbo")
    assert is_non_vision_model("gpt-3.5-turbo-0125")
    assert is_non_vision_model("gpt-4")
    assert is_non_vision_model("gpt-4-0613")
    assert is_non_vision_model("gpt-4-32k")
    assert is_non_vision_model("o1-mini")
    assert is_non_vision_model("o3-mini-2025-01-31")
    assert is_non_vision_model("gpt-live-1")
    assert is_non_vision_model("gpt-live-2")


def test_vision_models_not_excluded():
    assert not is_non_vision_model("gpt-4o")
    assert not is_non_vision_model("gpt-4o-2024-11-20")
    assert not is_non_vision_model("gpt-4o-mini")
    assert not is_non_vision_model("gpt-4-turbo")
    assert not is_non_vision_model("gpt-4.1")
    assert not is_non_vision_model("gpt-4.1-mini")
    assert not is_non_vision_model("gpt-5")
    assert not is_non_vision_model("gpt-5-codex")
    assert not is_non_vision_model("gpt-5.1-codex-mini")
    assert not is_non_vision_model("gpt-6-astra")
    assert not is_non_vision_model("o3")
    assert not is_non_vision_model("o4-mini")
    assert not is_non_vision_model("o1")
    assert not is_non_vision_model("o1-pro")

def test_non_chat_candidates_filtered():
    assert not is_chat_model_candidate("text-embedding-3-large")
    assert not is_chat_model_candidate("whisper-1")
    assert not is_chat_model_candidate("gpt-4o-search-preview")
    assert not is_chat_model_candidate("ft:gpt-4o:org:custom")
    assert is_chat_model_candidate("gpt-4o")
    assert is_chat_model_candidate("gpt-5.4-mini")
