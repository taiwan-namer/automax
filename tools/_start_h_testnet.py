#!/usr/bin/env python3
"""Start H surge-trend v1 on Binance Futures Testnet.

Binance wiring aligned with E (adapter/bridge/clientOrderId/symbol lock/exchange guard).
Does NOT auto-run unless operator invokes this script with ACK envs.
Does not touch A/E runners or flatten existing positions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "output" / "testnet_abcd_setup" / "code"
LIVE = ROOT / "output" / "testnet_h_local"
DATA = LIVE / "data_h"
LOG = LIVE / "logs" / "strat_h_testnet.log"
PID_FILE = LIVE / "pids" / "H_TN.pid"
LOCK = ROOT / "output" / "testnet_active_symbols_h.json"
ENV_H = ROOT / ".env.testnet.h"
ENV_DEFAULT = ROOT / ".env.testnet"

PROFILE_PARENT = "testnet_h_surge_trend_v1"
PROFILE_ID = "testnet_h_surge_trend_v1_ledger"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return str(pid) in (r.stdout or "")


def main() -> int:
    required = (
        "AUTOMAX_H_ACK_SPEC",
        "AUTOMAX_H_ACK_NO_EXCHANGE_STOP",
        "AUTOMAX_H_ACK_LIMIT_EXIT",
        "AUTOMAX_H_ACK_TESTNET",
    )
    missing = [k for k in required if (os.environ.get(k) or "").strip() != "1"]
    if missing:
        print("ERROR: set ACK env before Testnet start:", flush=True)
        for k in missing:
            print(f"  {k}=1", flush=True)
        return 2

    env_file = ENV_H if ENV_H.is_file() else ENV_DEFAULT
    if not env_file.is_file():
        print(f"ERROR: missing credentials file ({ENV_H} or {ENV_DEFAULT})", flush=True)
        return 2

    if PID_FILE.is_file():
        try:
            old = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            old = 0
        if _pid_alive(old):
            print(f"ERROR: H testnet already running pid={old}", flush=True)
            return 1

    (LIVE / "pids").mkdir(parents=True, exist_ok=True)
    (LIVE / "logs").mkdir(parents=True, exist_ok=True)
    (LIVE / "order_fill_ledger").mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    for name in (PROFILE_PARENT, PROFILE_ID):
        arm_root = DATA / "output" / name
        for p in (arm_root / "paper_profile.lock", arm_root / "paper.pid"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    env = os.environ.copy()
    env["PYTHONPATH"] = str(CODE) + os.pathsep + str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["ENABLE_BINANCE_TESTNET_ORDER"] = "1"
    env["AUTOMAX_TESTNET_MODE"] = "1"
    env["AUTOMAX_TESTNET_ENV_FILE"] = str(env_file.resolve())
    env["AUTOMAX_SYMBOL_LOCK_PATH"] = str(LOCK.resolve())
    env["BREAKOUT_RUNTIME_ROOT"] = str(DATA.resolve())
    env["AUTOMAX_ORDER_LEDGER_DIR"] = str((LIVE / "order_fill_ledger").resolve())
    env["AUTOMAX_H_PROFILE_PARENT"] = PROFILE_PARENT
    env["AUTOMAX_H_PROFILE_ID"] = PROFILE_ID

    fh = open(LOG, "a", encoding="utf-8")
    fh.write(f"\n===== launch {time.strftime('%Y-%m-%d %H:%M:%S')} TESTNET H v1 =====\n")
    args = [
        sys.executable,
        "-m",
        "h_surge_trend_v1.paper_main",
        "--paper",
        "--testnet",
        "--runtime-root",
        str(DATA.resolve()),
        "--hours",
        "0",
    ]
    fh.write("CMD: " + " ".join(args) + "\n")
    fh.write(f"ENV_FILE={env_file} LOCK={LOCK} prefix=STRAT_H_\n")
    fh.flush()

    cf = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform.startswith("win") else 0
    proc = subprocess.Popen(
        args,
        cwd=str(CODE),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        creationflags=cf,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    meta = {
        "mode": "binance_futures_testnet",
        "strategy": "H",
        "client_order_prefix": "STRAT_H_",
        "started_at": time.time(),
        "pid": proc.pid,
        "data": str(DATA),
        "log": str(LOG),
        "env_file": str(env_file),
        "symbol_lock": str(LOCK),
        "profile_parent": PROFILE_PARENT,
        "profile_id": PROFILE_ID,
        "note": (
            "H Testnet: trail STOP_MARKET sync + reduceOnly limit exit; "
            "no market fallback for strategy exit; ExchangeGuard LONG+SHORT; "
            "A/E untouched"
        ),
        "exchange_protection": "trail_stop_market_closePosition_MARK_PRICE",
    }
    (LIVE / "launch_meta_h_testnet.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[H-testnet] pid={proc.pid} log={LOG}", flush=True)
    time.sleep(6)
    alive = _pid_alive(proc.pid)
    print(f"[H-testnet] alive={alive}", flush=True)
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]:
        print(line, flush=True)
    return 0 if alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
