from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import config


def now_ts() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now(ZoneInfo(config.TZ_NAME)).isoformat()


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    n = int(qty / step + 1e-12)
    return round(n * step, 12)


def round_price_tick(px: float, tick: float) -> float:
    if tick <= 0:
        return px
    n = round(px / tick)
    return round(n * tick, 12)
