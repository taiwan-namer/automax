#!/usr/bin/env python3
"""Paper entry for H surge-trend v1. --paper required. Does not touch A/E."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import warnings
from pathlib import Path
from typing import Optional, TextIO

warnings.filterwarnings("ignore", category=FutureWarning)

from h_surge_trend_v1.win_lock import lock_ex_nb, unlock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h_surge_trend_v1 import config
from h_surge_trend_v1.engine import run_paper_loop
from h_surge_trend_v1.util import ensure_dirs, now_iso

_LOCK_FH: Optional[TextIO] = None


def _acquire_lock() -> None:
    global _LOCK_FH
    paths = config.shared_paths()
    ensure_dirs(paths["root"])
    _LOCK_FH = open(paths["lock"], "a+", encoding="utf-8")
    try:
        lock_ex_nb(_LOCK_FH)
    except BlockingIOError as exc:
        raise SystemExit(f"ERROR: another H paper holds lock: {paths['lock']}") from exc
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
            unlock(_LOCK_FH)
            _LOCK_FH.close()
        except Exception:
            pass
        _LOCK_FH = None


def main() -> None:
    parser = argparse.ArgumentParser(description="H surge-trend v1 paper")
    parser.add_argument("--paper", action="store_true", required=True)
    parser.add_argument("--runtime-root", type=str, required=True)
    parser.add_argument("--hours", type=float, default=0.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--testnet", action="store_true", help="NOT authorized in H v1 delivery by default")
    parser.add_argument(
        "--allow-unconfirmed",
        action="store_true",
        help="offline/test only: bypass launch blockers",
    )
    args = parser.parse_args()
    if getattr(args, "testnet", False):
        os.environ["ENABLE_BINANCE_TESTNET_ORDER"] = "1"
    # Paper/Testnet: allow AUTOMAX_H_PROFILE_* to keep existing ledger across version bumps
    config.apply_testnet_profile_env()
    if not args.paper:
        print("ERROR: --paper required", file=sys.stderr)
        raise SystemExit(2)

    config.set_runtime_root(args.runtime_root)

    blockers = config.launch_blockers()
    if blockers and not (args.once and args.allow_unconfirmed):
        print("ERROR: launch blocked — confirm before continuous start:", file=sys.stderr)
        for b in blockers:
            print(f"  - {b}", file=sys.stderr)
        raise SystemExit(3)

    _acquire_lock()
    paths = config.shared_paths()
    paths["pid"].write_text(str(os.getpid()), encoding="utf-8")
    from h_surge_trend_v1.util import atomic_write_json, file_sha256

    pkg = Path(__file__).resolve().parent
    code_files = sorted(pkg.glob("*.py"))
    hashes = {f.name: file_sha256(f)[:16] for f in code_files}
    atomic_write_json(
        paths["launch"],
        {
            "launched_at": now_iso(),
            "pid": os.getpid(),
            "strategy_id": config.STRATEGY_ID,
            "strategy_version": config.STRATEGY_VERSION,
            "config_hash": config.config_hash(),
            "code_hashes_sha16": hashes,
            "profile_parent": config.PROFILE_PARENT,
            "profile_id": config.PROFILE_ID,
            "simulation_assumptions": config.SIMULATION_ASSUMPTIONS,
            "exchange_protection_active": False,
            "allow_unconfirmed": bool(args.allow_unconfirmed),
            "testnet": bool(args.testnet),
        },
    )
    print(f"[{now_iso()}] H price_action_v2 paper 入口", flush=True)
    print(f"版本：{config.STRATEGY_VERSION}｜config_hash={config.config_hash()[:16]}", flush=True)
    print(f"profile：{config.PROFILE_PARENT} / {config.PROFILE_ID}", flush=True)
    print(f"資料根：{config.RUNTIME_ROOT}", flush=True)
    print(
        f"費用假設：Maker {config.MAKER_FEE_RATE}｜Taker {config.TAKER_FEE_RATE}｜"
        f"模擬成交≠交易所實際",
        flush=True,
    )
    if args.testnet:
        print("說明：Testnet flag set — still requires separate protection approval.", flush=True)
    else:
        print("說明：模擬／公開行情；不會呼叫實盤下單 API。", flush=True)

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
