from __future__ import annotations

import json
import math
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
            f.flush()
            os.fsync(f.fileno())
        last: OSError | None = None
        for attempt in range(12):
            try:
                os.replace(tmp, path)
                last = None
                break
            except PermissionError as exc:
                last = exc
                time.sleep(0.05 * (attempt + 1))
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (5, 32) and getattr(exc, "errno", None) not in (13,):
                    raise
                last = exc
                time.sleep(0.05 * (attempt + 1))
        if last is not None:
            raise last
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
    """Floor to step so notional does not exceed target after sizing."""
    if step <= 0:
        return qty
    n = int(qty / step + 1e-12)
    return round(n * step, 12)


def round_price_tick(px: float, tick: float) -> float:
    if tick <= 0:
        return px
    n = round(px / tick)
    return round(n * tick, 12)


def floor_price_tick(px: float, tick: float) -> float:
    if tick <= 0:
        return px
    n = math.floor(px / tick + 1e-12)
    return round(n * tick, 12)


def ceil_price_tick(px: float, tick: float) -> float:
    if tick <= 0:
        return px
    n = math.ceil(px / tick - 1e-12)
    return round(n * tick, 12)


def file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
