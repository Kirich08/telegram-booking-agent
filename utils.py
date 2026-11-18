"""Small helper functions shared across the project."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    LONDON = ZoneInfo("Europe/London")
except ZoneInfoNotFoundError as exc:
    # Windows has no built-in time zone database. The tzdata package provides it.
    raise SystemExit(
        "The time zone database is missing. Run: pip install tzdata\n"
        "(it is listed in requirements.txt)"
    ) from exc


def get_current_time_london() -> datetime:
    """Return the current time as a timezone-aware datetime in London."""
    return datetime.now(LONDON)


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """Replace characters that are unsafe in filenames with underscores."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (cleaned or "file")[:max_length]


def ensure_directory(path: str | Path) -> Path:
    """Create the directory (and parents) if it does not exist, return it as Path.

    If a file path is given, its parent directory is created instead.
    """
    p = Path(path)
    target = p.parent if p.suffix else p
    target.mkdir(parents=True, exist_ok=True)
    return p
