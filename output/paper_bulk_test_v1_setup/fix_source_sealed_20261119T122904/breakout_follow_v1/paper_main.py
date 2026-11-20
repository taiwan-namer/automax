#!/usr/bin/env python3
"""
breakout_follow_v1 專用 paper 入口（支援獨立 profile）。

用法：
  BREAKOUT_RUNTIME_ROOT=/abs/data_root \\
    python3 -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 --hours 0
  python3 -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 --once
  python3 -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 --stop

注意：
  --once 會跑完整一輪掃描並可模擬下單，不是「純初始化」。
  長跑請用獨立 runtime 目錄（BREAKOUT_RUNTIME_ROOT），勿在可改動 worktree 上跑。
  同 profile 互斥以 flock 原子鎖為準；PID 檔僅輔助停止／驗收。
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, TextIO

# allow running as module from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from breakout_follow_v1 import config
from breakout_follow_v1.account import load_or_create_account, save_account
from breakout_follow_v1.engine import run_paper_loop
from breakout_follow_v1.profiles import DEFAULT_PROFILE, list_profiles
from breakout_follow_v1.safety import LiveTradingForbidden, scan_source_for_order_calls
from breakout_follow_v1.util import ensure_dirs, now_iso

_STOP = False
_LOCK_FH: Optional[TextIO] = None


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


def _profile_from_cmdline(cmdline: str) -> str:
    parts = cmdline.split()
    for i, p in enumerate(parts):
        if p == "--profile" and i + 1 < len(parts):
            return parts[i + 1]
        if p.startswith("--profile="):
            return p.split("=", 1)[1]
    return DEFAULT_PROFILE


def _find_conflicting_processes(active_profile: str) -> list[str]:
    """僅阻擋「同一 profile／同一帳戶路徑」的 paper 程序；允許與其他 profile 並行。"""
    hits: list[str] = []
    me = {os.getpid(), os.getppid()}
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
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
            if "python" not in exe.lower():
                continue
            cmdline = " ".join(parts[10:])
            if "main.py --live" in cmdline or "live_paper_trader" in cmdline:
                hits.append(line.strip())
                continue
            if "breakout_follow_v1.paper_main" in cmdline or "breakout_follow_v1/paper_main" in cmdline:
                other = _profile_from_cmdline(cmdline)
                if other == active_profile:
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
        if p.name in ("safety.py", "config.py", "paper_main.py", "profiles.py"):
            continue
        if hits:
            bad.append(f"{p.name}: {hits}")
    return bad


def acquire_profile_lock(active_profile: str) -> tuple[bool, str]:
    """
    同 profile 原子互斥：對 LOCK_PATH 取排他 flock(LOCK_EX|LOCK_NB)。
    成功後持有至進程結束；失敗表示另一實例已持鎖（無 TOCTOU 僅查 PID 窗口）。
    """
    global _LOCK_FH
    ensure_dirs()
    lock_path = Path(config.LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = ""
        try:
            fh.seek(0)
            holder = fh.read().strip()
        except Exception:
            holder = ""
        fh.close()
        return False, f"profile lock held: {lock_path} holder={holder or 'unknown'}"
    except OSError as exc:
        fh.close()
        return False, f"profile lock acquire failed: {exc}"

    meta = {
        "pid": os.getpid(),
        "profile": active_profile,
        "PROFILE_ID": config.PROFILE_ID,
        "RUNTIME_ROOT": str(Path(config.RUNTIME_ROOT).resolve()),
        "ROOT_OUT": str(Path(config.ROOT_OUT).resolve()),
        "acquired_at": now_iso(),
        "argv": sys.argv,
    }
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(meta, ensure_ascii=False))
    fh.flush()
    os.fsync(fh.fileno())
    _LOCK_FH = fh

    def _release() -> None:
        global _LOCK_FH
        if _LOCK_FH is None:
            return
        try:
            fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            _LOCK_FH.close()
        except Exception:
            pass
        _LOCK_FH = None

    atexit.register(_release)
    return True, str(lock_path)


def preflight(active_profile: str) -> tuple[bool, list[str]]:
    msgs: list[str] = []
    ok = True
    # 1) 原子鎖（主互斥）
    locked, lock_msg = acquire_profile_lock(active_profile)
    if not locked:
        ok = False
        msgs.append(lock_msg)
    else:
        msgs.append(f"profile flock acquired: {lock_msg}")

    # 2) 進程掃描（輔助，非唯一互斥）
    conflicts = _find_conflicting_processes(active_profile)
    if conflicts:
        if any(c.startswith("PROCESS_CHECK_FAILED") for c in conflicts):
            msgs.append("進程檢查警告：" + "; ".join(conflicts))
        else:
            # 若 flock 已拿到但仍見同 profile 進程，視為異常殘留（可能舊版無 flock）
            ok = False
            msgs.append(f"發現同 profile={active_profile} 的既有 paper 進程，拒絕自動啟動：")
            msgs.extend(conflicts[:10])

    if config.PID_PATH.exists():
        try:
            file_pid = int(config.PID_PATH.read_text(encoding="utf-8").strip())
            alive = subprocess.run(["ps", "-p", str(file_pid), "-o", "command="], capture_output=True, text=True)
            if alive.returncode == 0 and alive.stdout.strip():
                cmd = alive.stdout.strip()
                if "breakout_follow_v1.paper_main" in cmd and _profile_from_cmdline(cmd) == active_profile:
                    if file_pid != os.getpid():
                        ok = False
                        msgs.append(f"PID 檔指向仍存活的同 profile 進程: {file_pid}")
        except Exception as exc:
            msgs.append(f"PID 檔檢查警告: {exc}")

    bad = _static_safety_scan()
    if bad:
        ok = False
        msgs.append("原始碼含可疑下單字串：" + "; ".join(bad))
    ensure_dirs()
    if config.ACCOUNT_PATH.exists():
        try:
            acc = json.loads(config.ACCOUNT_PATH.read_text(encoding="utf-8"))
            if acc.get("inconsistency"):
                ok = False
                msgs.append(f"帳戶狀態不一致：{acc.get('inconsistency')} — 拒絕新倉/啟動")
        except Exception as exc:
            ok = False
            msgs.append(f"無法讀取帳戶：{exc}")
    return ok, msgs


def _process_cmdline(pid: int) -> Optional[str]:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    cmd = (r.stdout or "").strip()
    return cmd or None


def stop_profile(active_profile: str, *, timeout_sec: float = 30.0) -> dict[str, Any]:
    """
    停止同 profile 程序：必須同時核對 PID 檔、進程存活、cmdline 含 paper_main、profile 相符。
    不可只信舊 pid 檔內容。
    """
    report: dict[str, Any] = {
        "action": "stop",
        "profile": active_profile,
        "PROFILE_ID": config.PROFILE_ID,
        "pid_path": str(config.PID_PATH.resolve()),
        "ok": False,
    }
    if not config.PID_PATH.exists():
        report["error"] = "PID file missing"
        return report
    try:
        file_pid = int(config.PID_PATH.read_text(encoding="utf-8").strip())
    except Exception as exc:
        report["error"] = f"PID file unreadable: {exc}"
        return report
    report["file_pid"] = file_pid
    cmd = _process_cmdline(file_pid)
    if cmd is None:
        report["error"] = "PID not alive; refusing to trust stale pid file alone"
        report["stale_pid_file"] = True
        return report
    report["cmdline"] = cmd
    if "breakout_follow_v1.paper_main" not in cmd and "breakout_follow_v1/paper_main" not in cmd:
        report["error"] = "PID alive but cmdline is not breakout paper_main — refuse kill"
        return report
    got = _profile_from_cmdline(cmd)
    report["cmdline_profile"] = got
    if got != active_profile:
        report["error"] = f"profile mismatch: file/cmd={got} requested={active_profile}"
        return report
    os.kill(file_pid, signal.SIGTERM)
    report["signaled"] = "SIGTERM"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _process_cmdline(file_pid) is None:
            report["ok"] = True
            report["stopped"] = True
            try:
                config.PID_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            return report
        time.sleep(0.2)
    report["error"] = "timeout waiting for process exit after SIGTERM"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="breakout_follow_v1 paper trader")
    parser.add_argument(
        "--paper",
        action="store_true",
        required=True,
        help="必須明確指定：僅模擬，禁止實盤",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=DEFAULT_PROFILE,
        choices=list_profiles(),
        help=f"paper profile（預設 {DEFAULT_PROFILE}；bulk 用 paper_bulk_test_v1）",
    )
    parser.add_argument(
        "--runtime-root",
        type=str,
        default=None,
        help="絕對資料根（等同 BREAKOUT_RUNTIME_ROOT）；長跑必填以避免 cwd 漂移",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="跑完整一輪掃描（含模擬下單可能）；不是純初始化",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=6.0,
        help="觀察窗口小時（預設6；0=持續至手動停止；到時不強制平倉）",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="核對 PID+profile 後送 SIGTERM；不啟動",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="危險：跳過進程／鎖檢查（測試用）")
    args = parser.parse_args(argv)

    if not args.paper:
        print("ERROR: --paper is required", file=sys.stderr)
        return 2

    if args.runtime_root:
        os.environ["BREAKOUT_RUNTIME_ROOT"] = str(Path(args.runtime_root).resolve())
        config.set_runtime_root(args.runtime_root)
    elif os.environ.get("BREAKOUT_RUNTIME_ROOT"):
        config.set_runtime_root(os.environ["BREAKOUT_RUNTIME_ROOT"])

    applied = config.apply_profile(args.profile)
    print(f"[{now_iso()}] breakout_follow_v1 paper entry")
    print(f"profile: {args.profile} → PROFILE_ID={config.PROFILE_ID}")
    print(f"RUNTIME_ROOT: {Path(config.RUNTIME_ROOT).resolve()}")
    print(f"output dir: {Path(config.ROOT_OUT).resolve()}")
    print(f"vault dir: {Path(config.KLINE_VAULT_DIR).resolve()}")
    print(f"account: {Path(config.ACCOUNT_PATH).resolve()}")
    print(f"lock: {Path(config.LOCK_PATH).resolve()}")
    print(f"CODE_FIX_TAG: {config.CODE_FIX_TAG}")
    print(f"apply_profile: {applied}")
    print("SIMULATION_ASSUMPTION fees/slippage — not live trading")

    if args.stop:
        rep = stop_profile(args.profile)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep.get("ok") else 1

    if args.once:
        print("NOTE: --once runs a full scan cycle and MAY simulate opens/closes; not pure init")

    if not args.skip_preflight:
        ok, msgs = preflight(args.profile)
        for m in msgs:
            print(m)
        if not ok:
            print("PREFLIGHT FAILED — not starting")
            return 1
    else:
        print("WARNING: preflight skipped (no flock)")

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    cmd = [sys.executable, "-u", "-m", "breakout_follow_v1.paper_main", "--paper", "--profile", args.profile]
    if args.runtime_root or os.environ.get("BREAKOUT_RUNTIME_ROOT"):
        cmd.extend(["--runtime-root", str(Path(config.RUNTIME_ROOT).resolve())])
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
