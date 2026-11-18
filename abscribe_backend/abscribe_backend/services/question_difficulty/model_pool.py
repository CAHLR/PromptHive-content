"""Model pool configuration for multi-model respondent generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass

PROVIDER_OPENAI = "openai"
PROVIDER_REPLICATE = "replicate"
PROVIDER_GOOGLE = "google"
PROVIDER_COHERE = "cohere"

SUPPORTED_PROVIDERS = frozenset(
    {PROVIDER_OPENAI, PROVIDER_REPLICATE, PROVIDER_GOOGLE, PROVIDER_COHERE}
)


@dataclass(frozen=True)
class ModelPoolEntry:
    model_name: str
    provider: str
    count: int
    temperature: float


# Edit this list to rebalance counts or add models/providers.
DEFAULT_MODEL_POOL: tuple[ModelPoolEntry, ...] = (
    ModelPoolEntry("gpt-4o", PROVIDER_OPENAI, 10, 0.7),
    ModelPoolEntry("gpt-4-turbo", PROVIDER_OPENAI, 10, 0.7),
    ModelPoolEntry("gpt-3.5-turbo", PROVIDER_OPENAI, 15, 0.7),
    ModelPoolEntry("gpt-4o-mini", PROVIDER_OPENAI, 15, 0.7),
)


def pool_total_respondents(pool: tuple[ModelPoolEntry, ...] = DEFAULT_MODEL_POOL) -> int:
    return sum(entry.count for entry in pool)


def pool_to_manifest(pool: tuple[ModelPoolEntry, ...] = DEFAULT_MODEL_POOL) -> list[dict]:
    """Serialize pool for manifest.json."""
    return [asdict(entry) for entry in pool]
