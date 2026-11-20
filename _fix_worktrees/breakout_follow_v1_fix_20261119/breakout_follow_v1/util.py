"""Shared helpers: time, hash, jsonl, atomic write."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config

TZ = ZoneInfo(config.TZ_NAME)


def now_ts() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now(tz=TZ).isoformat(timespec="seconds")


def today_key() -> str:
    return datetime.now(tz=TZ).strftime("%Y-%m-%d")


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=TZ).isoformat(timespec="seconds")


def parse_iso_ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def stable_config_payload() -> dict[str, Any]:
    keys = sorted(k for k in dir(config) if k.isupper() and not k.startswith("_"))
    out: dict[str, Any] = {}
    for k in keys:
        v = getattr(config, k)
        if isinstance(v, Path):
            out[k] = str(v)
        elif isinstance(v, frozenset):
            out[k] = sorted(v)
        elif isinstance(v, set):
            out[k] = sorted(v)
        elif isinstance(v, dict):
            out[k] = {str(kk): vv for kk, vv in sorted(v.items(), key=lambda x: str(x[0]))}
        else:
            out[k] = v
    return out


def config_hash_v1() -> str:
    raw = json.dumps(stable_config_payload(), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{config.CONFIG_HASH_ALGO}:{raw}".encode()).hexdigest()[:16]


def atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def report_error(code: str, detail: str = "", *, symbol: str = "") -> None:
    """Explicit error report: always stderr; also run data_quality when a run store is active."""
    import sys

    print(f"[breakout_follow_v1][{code}] {symbol} {detail}".rstrip(), file=sys.stderr)
    try:
        from .run_store import get_store

        store = get_store()
        if store is not None:
            store.note_quality(code, symbol=symbol, detail=detail, replay_precise=False)
    except Exception as exc:  # the stderr line above already carries the original error
        print(f"[breakout_follow_v1][REPORT_ERROR_FAILED] {code}: {exc}", file=sys.stderr)


def ensure_dirs() -> None:
    config.ROOT_OUT.mkdir(parents=True, exist_ok=True)
    config.KLINE_VAULT_DIR.mkdir(parents=True, exist_ok=True)
    config.DECISION_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
