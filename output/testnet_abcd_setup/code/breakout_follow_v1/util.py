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
    """Write JSON via temp+replace. On Windows, briefly retry WinError 5 (file in use)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
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


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """Append one JSONL row with bounded retries (visible failure after exhaustion)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
    last: OSError | None = None
    for attempt in range(8):
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05 * (attempt + 1))
    raise OSError(f"append_jsonl_failed path={path} attempts=8 last={last}")


def ensure_dirs() -> None:
    config.ROOT_OUT.mkdir(parents=True, exist_ok=True)
    config.KLINE_VAULT_DIR.mkdir(parents=True, exist_ok=True)
    config.DECISION_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
