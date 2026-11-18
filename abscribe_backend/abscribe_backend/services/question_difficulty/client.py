"""OpenAI API client for synthetic respondents (delegates to provider layer)."""
from typing import Optional

from .llm_providers import CompletionResult, ask_provider, ask_provider_async
from .model_pool import PROVIDER_OPENAI


def ask_llm(
    prompt: str | list,
    seed: int,
    temperature: float = 0.5,
    model_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    provider: str = PROVIDER_OPENAI,
    respondent_id: str | None = None,
    item_id: str | None = None,
    max_tokens: int | None = None,
) -> CompletionResult:
    """Query LLM via provider router (OpenAI by default).

    ``max_tokens=None`` keeps the provider default (respondent token cap).
    """
    import os

    model = model_name or os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    return ask_provider(
        provider=provider,
        prompt=prompt,
        seed=seed,
        temperature=temperature,
        model_name=model,
        system_prompt=system_prompt,
        respondent_id=respondent_id,
        item_id=item_id,
        max_tokens=max_tokens,
    )


async def ask_llm_async(
    prompt: str | list,
    seed: int,
    temperature: float = 0.5,
    model_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    provider: str = PROVIDER_OPENAI,
    respondent_id: str | None = None,
    item_id: str | None = None,
    max_tokens: int | None = None,
) -> CompletionResult:
    """Async LLM query via provider router.

    ``max_tokens=None`` keeps the provider default (respondent token cap).
    """
    import os

    model = model_name or os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    return await ask_provider_async(
        provider=provider,
        prompt=prompt,
        seed=seed,
        temperature=temperature,
        model_name=model,
        system_prompt=system_prompt,
        respondent_id=respondent_id,
        item_id=item_id,
        max_tokens=max_tokens,
    )
