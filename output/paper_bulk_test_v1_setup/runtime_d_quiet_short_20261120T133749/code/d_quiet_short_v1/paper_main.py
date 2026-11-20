#!/usr/bin/env python3
"""Paper entry for D quiet-short. --paper required. Never touches A/B/C."""

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

from d_quiet_short_v1 import config
from d_quiet_short_v1.engine import run_paper_loop
from d_quiet_short_v1.util import ensure_dirs, now_iso

_LOCK_FH: Optional[TextIO] = None


def _acquire_lock() -> None:
    global _LOCK_FH
    paths = config.paths()
    ensure_dirs(paths["root"])
    _LOCK_FH = open(paths["lock"], "a+", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit(f"ERROR: another D quiet-short paper holds lock: {paths['lock']}") from exc
    meta = {
        "pid": os.getpid(),
        "profile": config.PROFILE_ID,
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
        if "python" not in parts[10].lower():
            continue
        if "-m d_quiet_short_v1.paper_main" in cmdline or "d_quiet_short_v1/paper_main.py" in cmdline:
            hits.append(line.strip())
    if hits:
        raise SystemExit("ERROR: duplicate D quiet-short executor:\n" + "\n".join(hits))


def main() -> None:
    parser = argparse.ArgumentParser(description="D quiet-short paper")
    parser.add_argument("--paper", action="store_true", required=True)
    parser.add_argument("--runtime-root", type=str, required=True)
    parser.add_argument("--hours", type=float, default=0.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Force scan-only (no paper fills). Default when capital/depth unconfirmed.",
    )
    parser.add_argument(
        "--allow-fills",
        action="store_true",
        help="Request paper fills (still blocked unless capital+depth confirmed).",
    )
    args = parser.parse_args()
    if not args.paper:
        print("ERROR: --paper required", file=sys.stderr)
        raise SystemExit(2)

    config.set_runtime_root(args.runtime_root)
    _check_no_duplicate()
    _acquire_lock()
    paths = config.paths()
    paths["pid"].write_text(str(os.getpid()), encoding="utf-8")

    allow_fills = bool(args.allow_fills) and not bool(args.scan_only)
    # Hard gate
    if allow_fills and not config.paper_fills_allowed():
        print(
            "ERROR: paper fills requested but capital/depth still UNCONFIRMED. "
            "Use --scan-only, or confirm settings first. Refusing fill mode.",
            file=sys.stderr,
        )
        _release_lock()
        raise SystemExit(3)

    from d_quiet_short_v1.util import atomic_write_json

    atomic_write_json(
        paths["launch"],
        {
            "launched_at": now_iso(),
            "pid": os.getpid(),
            "strategy_version": config.STRATEGY_VERSION,
            "mode": "PAPER_FILLS" if allow_fills else "SCAN_ONLY",
            "ttl_policy": config.TTL_POLICY,
            "hard_exclude": sorted(config.HARD_EXCLUDE_SYMBOLS),
            "pending": config.pending_confirmations(),
            "fee": config.FEE_RATE_ONE_SIDE,
            "slip": config.SLIPPAGE_ADVERSE,
            "fill_model": config.SIMULATION_ASSUMPTIONS,
        },
    )
    print(f"[{now_iso()}] D 低波動小幣做空 paper 入口", flush=True)
    print(f"版本：{config.STRATEGY_VERSION}", flush=True)
    print(f"資料根：{config.RUNTIME_ROOT}", flush=True)
    print(f"模式：{'PAPER_FILLS' if allow_fills else 'SCAN_ONLY'}", flush=True)
    print("說明：模擬；不會呼叫實盤下單 API。結果非實盤成果。", flush=True)

    def _stop(signum, frame):  # noqa: ARG001
        print(f"\n[停止] 收到 {signum}，保存後退出（不平殺）", flush=True)
        _release_lock()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        asyncio.run(run_paper_loop(hours=args.hours, once=args.once, allow_fills=allow_fills))
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
