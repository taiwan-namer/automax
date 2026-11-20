#!/usr/bin/env python3
"""
breakout_follow_v1 專用 paper 入口。

用法：
  python3 -m breakout_follow_v1.paper_main --paper
  python3 -m breakout_follow_v1.paper_main --paper --once
  python3 -m breakout_follow_v1.paper_main --paper --hours 6

禁止：不加 --paper 不得啟動；無私有下單 API。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# allow running as module from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import signal

from breakout_follow_v1 import config
from breakout_follow_v1.account import load_or_create_account, save_account
from breakout_follow_v1.engine import run_paper_loop
from breakout_follow_v1.safety import LiveTradingForbidden, scan_source_for_order_calls
from breakout_follow_v1.util import ensure_dirs, now_iso

_STOP = False


def _handle_stop(signum, frame) -> None:  # noqa: ARG001
    global _STOP
    _STOP = True
    try:
        st = load_or_create_account()
        save_account(st)
        print(f"\n[signal {signum}] account saved; exiting without force-flat")
    except Exception as exc:
        print(f"[signal {signum}] save failed: {exc}")
    raise SystemExit(0)


def _find_conflicting_processes() -> list[str]:
    hits: list[str] = []
    me = {os.getpid(), os.getppid()}
    try:
        import subprocess

        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
            # 解析 PID（ps aux: USER PID ... COMMAND 自第 11 欄起）
            parts = line.split()
            if len(parts) < 11:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            if pid in me:
                continue
            exe = parts[10]
            # 必須是 python 解譯器行程，排除 zsh wrapper 命令列裡碰巧含字串
            if "python" not in exe.lower():
                continue
            cmdline = " ".join(parts[10:])
            if "main.py --live" in cmdline or "live_paper_trader" in cmdline:
                hits.append(line.strip())
                continue
            if "breakout_follow_v1.paper_main" in cmdline or "breakout_follow_v1/paper_main" in cmdline:
                hits.append(line.strip())
    except Exception as exc:
        hits.append(f"PROCESS_CHECK_FAILED: {exc}")
    return hits


def _static_safety_scan() -> list[str]:
    pkg = Path(__file__).resolve().parent
    bad: list[str] = []
    for p in pkg.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        hits = scan_source_for_order_calls(src)
        # allow listing forbidden strings inside safety.py / config.py
        if p.name in ("safety.py", "config.py", "paper_main.py"):
            continue
        if hits:
            bad.append(f"{p.name}: {hits}")
    return bad


def preflight() -> tuple[bool, list[str]]:
    msgs: list[str] = []
    ok = True
    conflicts = _find_conflicting_processes()
    if conflicts:
        # 若僅 PROCESS_CHECK_FAILED 則停止
        if any(c.startswith("PROCESS_CHECK_FAILED") for c in conflicts):
            ok = False
            msgs.append("無法確認進程狀態：" + "; ".join(conflicts))
        else:
            # 其他交易程序存在 → 停止自動啟動
            ok = False
            msgs.append("發現既有交易相關進程，拒絕自動啟動：")
            msgs.extend(conflicts[:10])
    bad = _static_safety_scan()
    if bad:
        ok = False
        msgs.append("原始碼含可疑下單字串：" + "; ".join(bad))
    # account inconsistency
    ensure_dirs()
    if config.ACCOUNT_PATH.exists():
        import json

        try:
            acc = json.loads(config.ACCOUNT_PATH.read_text(encoding="utf-8"))
            if acc.get("inconsistency"):
                ok = False
                msgs.append(f"帳戶狀態不一致：{acc.get('inconsistency')} — 拒絕新倉/啟動")
        except Exception as exc:
            ok = False
            msgs.append(f"無法讀取帳戶：{exc}")
    return ok, msgs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="breakout_follow_v1 paper trader")
    parser.add_argument(
        "--paper",
        action="store_true",
        required=True,
        help="必須明確指定：僅模擬，禁止實盤",
    )
    parser.add_argument("--once", action="store_true", help="只跑一輪掃描")
    parser.add_argument(
        "--hours",
        type=float,
        default=6.0,
        help="觀察窗口小時（預設6；0=持續至手動停止；到時不強制平倉）",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="危險：跳過進程檢查（測試用）")
    args = parser.parse_args(argv)

    if not args.paper:
        print("ERROR: --paper is required", file=sys.stderr)
        return 2

    print(f"[{now_iso()}] breakout_follow_v1 paper entry")
    print(f"output dir: {config.ROOT_OUT.resolve()}")
    print("SIMULATION_ASSUMPTION fees/slippage — not live trading")

    if not args.skip_preflight:
        ok, msgs = preflight()
        for m in msgs:
            print(m)
        if not ok:
            print("PREFLIGHT FAILED — not starting")
            return 1
    else:
        print("WARNING: preflight skipped")

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    cmd = [sys.executable, "-u", "-m", "breakout_follow_v1.paper_main", "--paper"]
    if args.once:
        cmd.append("--once")
    else:
        cmd.extend(["--hours", str(args.hours)])

    try:
        asyncio.run(
            run_paper_loop(
                hours=0 if args.once else args.hours,
                once=args.once,
                launch_command=cmd,
            )
        )
    except LiveTradingForbidden as exc:
        print(f"BLOCKED live path: {exc}", file=sys.stderr)
        return 3
    except SystemExit:
        return 0
    except KeyboardInterrupt:
        print("interrupted; account state preserved")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
