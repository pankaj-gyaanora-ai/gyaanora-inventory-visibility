"""Config loading. The availability formula lives here, in YAML, not in code."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml


def load_config(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    cfg["_config_path"] = str(path)
    cfg["_root"] = str(Path(path).resolve().parent.parent)
    return cfg


def parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
