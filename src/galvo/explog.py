"""Append-only exposure log: one JSON object per line, one line per exposure.

Every record is flushed and synced to disk before the call returns, so a crash cannot
lose an exposure that already happened. Records are built by the sequencer; see
``galvo.sequencer.ExposureResult.to_record`` for the fields.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class ExposureLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, allow_nan=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read(self) -> list[dict[str, Any]]:
        return read_log(self.path)


def read_log(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
