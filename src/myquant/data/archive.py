from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


class RawArchiver:
    """Stores immutable vendor responses outside the normalized database."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def archive(
        self, endpoint: str, params: dict[str, Any], frame: pd.DataFrame, source: str = "tushare"
    ) -> tuple[Path, str]:
        payload = frame.to_csv(index=False).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        day = datetime.now(UTC).strftime("%Y%m%d")
        params_digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]
        directory = self.root / source / endpoint / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{params_digest}-{digest[:12]}.csv"
        if not path.exists():
            path.write_bytes(payload)
        return path, digest
