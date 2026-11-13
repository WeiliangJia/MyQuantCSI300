from __future__ import annotations

from pathlib import Path

import pandas as pd


DEFAULT_EXCLUSION_PATH = Path("data/reference/research_exclusions.csv")


def load_excluded_ts_codes(path: Path = DEFAULT_EXCLUSION_PATH) -> set[str]:
    """Load the auditable do-not-research list; raw vendor data is retained."""
    if not path.exists():
        return set()
    frame = pd.read_csv(path, dtype=str).fillna("")
    if "ts_code" not in frame:
        raise ValueError(f"Exclusion file must contain ts_code: {path}")
    active = frame.loc[frame.get("active", "true").astype(str).str.lower().isin({"true", "1", "yes"})]
    return set(active["ts_code"].str.upper().str.strip())
