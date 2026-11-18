"""I/O utilities for loading items and saving results."""
import pandas as pd
import re


def load_items(path: str) -> pd.DataFrame:
    """Load items CSV and validate required columns."""
    df = pd.read_csv(path)
    required = {'item_id', 'stem', 'answer_key', 'answer_type'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Validate answer_type values
    valid_types = {'numeric', 'algebra', 'mc', 'string'}
    invalid = set(df['answer_type'].unique()) - valid_types
    if invalid:
        raise ValueError(f"Invalid answer_type values: {invalid}. Must be one of {valid_types}")

    # Ensure mc_choices exists for mc items
    if 'mc_choices' not in df.columns:
        df['mc_choices'] = None

    mc_items = df[df['answer_type'] == 'mc']
    if len(mc_items) > 0 and mc_items['mc_choices'].isna().any():
        raise ValueError("Items with answer_type='mc' must have mc_choices")

    return df


def save_csv(df: pd.DataFrame, path: str):
    """Save DataFrame to CSV."""
    df.to_csv(path, index=False, escapechar="\\", doublequote=True)
    print(f"Saved: {path}")


def clean_text(s: str) -> str:
    """Light cleanup shared by all answer types (no type-specific extraction)."""
    if not isinstance(s, str):
        return str(s)

    s = s.strip()

    # Persona respondent codes leaked into answers (e.g. "R031: x = -5")
    s = re.sub(r"^R\d+:\s*", "", s, flags=re.IGNORECASE)

    # Remove common prefixes
    s = re.sub(r"^(Answer:\s*|A:\s*|Solution:\s*)", "", s, flags=re.IGNORECASE)

    # Remove LaTeX delimiters
    s = re.sub(r"\$+", "", s)

    return " ".join(s.split())
