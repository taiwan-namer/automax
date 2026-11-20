#!/usr/bin/env python3
"""Paper entry for A surge-fade tri-arm. --paper required. Never touches B/C."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a_surge_fade_v1 import config
from a_surge_fade_v1.engine import run_paper_loop
from a_surge_fade_v1.util import ensure_dirs, now_iso

_LOCK_FH: Optional[TextIO] = None


def _acquire_lock() -> None:
    global _LOCK_FH
    paths = config.shared_paths()
    ensure_dirs(paths["root"])
    _LOCK_FH = open(paths["lock"], "a+", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit(f"ERROR: another A surge-fade paper holds lock: {paths['lock']}") from exc
    meta = {
        "pid": os.getpid(),
        "profile": config.PROFILE_PARENT,
        "strategy_version": config.STRATEGY_VERSION,
        "acquired_at": now_iso(),
        "runtime_root": str(config.RUNTIME_ROOT),
    }
    _LOCK_FH.seek(0)
    _LOCK_FH.truncate()
    _LOCK_FH.write(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    _LOCK_FH.flush()


def _release_lock() -> None:
    global _LOCK_FH
    if _LOCK_FH is not None:
        try:
            fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_UN)
            _LOCK_FH.close()
        except Exception:
            pass
        _LOCK_FH = None


def _check_no_duplicate() -> None:
    me = {os.getpid(), os.getppid()}
    r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
    hits = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 11:
            continue
        try:
            pid = int(parts[1])
        except Exception:
            continue
        if pid in me:
            continue
        cmdline = " ".join(parts[10:])
        # only real python module executors (ignore shells that merely mention the string)
        if "python" not in parts[10].lower():
            continue
        if "-m a_surge_fade_v1.paper_main" in cmdline or "a_surge_fade_v1/paper_main.py" in cmdline:
            hits.append(line.strip())
    if hits:
        raise SystemExit("ERROR: duplicate A surge-fade executor:\n" + "\n".join(hits))


def main() -> None:
    parser = argparse.ArgumentParser(description="A surge-fade paper (tri-arm)")
    parser.add_argument("--paper", action="store_true", required=True)
    parser.add_argument("--runtime-root", type=str, required=True)
    parser.add_argument("--hours", type=float, default=0.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.paper:
        print("ERROR: --paper required", file=sys.stderr)
        raise SystemExit(2)

    config.set_runtime_root(args.runtime_root)
    _check_no_duplicate()
    _acquire_lock()
    paths = config.shared_paths()
    paths["pid"].write_text(str(os.getpid()), encoding="utf-8")
    atomic_launch = {
        "launched_at": now_iso(),
        "pid": os.getpid(),
        "strategy_version": config.STRATEGY_VERSION,
        "arms": config.ARMS,
        "ttl_policy": config.TTL_POLICY,
        "hard_exclude": sorted(config.HARD_EXCLUDE_SYMBOLS),
        "fee": config.FEE_RATE_ONE_SIDE,
        "slip": config.SLIPPAGE_ADVERSE,
        "fill_model": "limit_touch_bid_ge_limit_fill_at_limit; no fabricate on stale quote",
    }
    from a_surge_fade_v1.util import atomic_write_json

    atomic_write_json(paths["launch"], atomic_launch)
    print(f"[{now_iso()}] A 暴漲轉弱做空 paper 入口", flush=True)
    print(f"版本：{config.STRATEGY_VERSION}", flush=True)
    print(f"資料根：{config.RUNTIME_ROOT}", flush=True)
    print("說明：模擬成交；不會呼叫實盤下單 API。", flush=True)

    def _stop(signum, frame):  # noqa: ARG001
        print(f"\n[停止] 收到 {signum}，保存後退出（不平殺）", flush=True)
        _release_lock()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        asyncio.run(run_paper_loop(hours=args.hours, once=args.once))
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
