#!/usr/bin/env python3
"""Paper entry for D quiet-short. --paper required. Never touches A/B/C."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional, TextIO

from d_quiet_short_v1.win_lock import lock_ex_nb, unlock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from d_quiet_short_v1 import config
from d_quiet_short_v1.engine import run_observe_loop, run_paper_loop
from d_quiet_short_v1.util import ensure_dirs, now_iso

_LOCK_FH: Optional[TextIO] = None


def _acquire_lock() -> None:
    global _LOCK_FH
    paths = config.paths()
    ensure_dirs(paths["root"])
    _LOCK_FH = open(paths["lock"], "a+", encoding="utf-8")
    try:
        lock_ex_nb(_LOCK_FH)
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
            unlock(_LOCK_FH)
            _LOCK_FH.close()
        except Exception:
            pass
        _LOCK_FH = None


def _check_no_duplicate() -> None:
    if sys.platform.startswith("win"):
        return
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
    parser.add_argument("--testnet", action="store_true", help="enable Binance Futures Testnet orders")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Force scan-only (no paper fills). Default when capital/depth unconfirmed.",
    )
    parser.add_argument(
        "--observe-only",
        action="store_true",
        help="Isolated observation: signals+shadow+quote/VOL; never OPEN/CLOSE; never exchange orders.",
    )
    parser.add_argument(
        "--allow-fills",
        action="store_true",
        help="Request paper fills (still blocked unless capital+depth confirmed).",
    )
    parser.add_argument("--run-id", type=str, default="", help="Observation/paper run id (required for observe-only).")
    parser.add_argument("--max-events", type=int, default=0, help="Stop observe-only after N observation_events lines.")
    parser.add_argument("--disk-limit-mb", type=float, default=512.0, help="Stop observe-only if log bytes exceed this.")
    args = parser.parse_args()
    if getattr(args, "testnet", False):
        import os as _os
        _os.environ["ENABLE_BINANCE_TESTNET_ORDER"] = "1"
    if not args.paper:
        print("ERROR: --paper required", file=sys.stderr)
        raise SystemExit(2)

    observe_only = bool(args.observe_only)
    if observe_only and args.allow_fills:
        print("ERROR: --observe-only cannot combine with --allow-fills", file=sys.stderr)
        raise SystemExit(2)
    if observe_only and args.testnet:
        print("ERROR: --observe-only cannot combine with --testnet (no exchange orders)", file=sys.stderr)
        raise SystemExit(2)
    if observe_only and args.scan_only:
        print("NOTE: --observe-only supersedes --scan-only", flush=True)

    from d_quiet_short_v1.observe import assert_isolated_runtime_root

    config.set_runtime_root(args.runtime_root)
    if observe_only:
        assert_isolated_runtime_root(config.RUNTIME_ROOT)

    _check_no_duplicate()
    _acquire_lock()
    paths = config.paths()
    paths["pid"].write_text(str(os.getpid()), encoding="utf-8")

    allow_fills = bool(args.allow_fills) and not bool(args.scan_only) and not observe_only
    # Hard gate
    if allow_fills and not config.paper_fills_allowed():
        print(
            "ERROR: paper fills requested but capital/depth still UNCONFIRMED. "
            "Use --scan-only/--observe-only, or confirm settings first. Refusing fill mode.",
            file=sys.stderr,
        )
        _release_lock()
        raise SystemExit(3)

    from d_quiet_short_v1.util import atomic_write_json

    run_id = args.run_id.strip()
    if observe_only and not run_id:
        run_id = "obs_" + now_iso().replace(":", "").replace("+", "_")[:18]
    settings = config.settings_snapshot_dict()
    settings["mode_flag"] = "OBSERVATION_ONLY" if observe_only else ("PAPER_FILLS" if allow_fills else "SCAN_ONLY")
    settings["settings_tag"] = "NEW_OBSERVATION_SETTINGS" if observe_only else settings.get("settings_tag")
    settings["run_id"] = run_id or None
    try:
        import subprocess

        git_cwd = ROOT
        settings["git_commit"] = None
        for _ in range(6):
            if (git_cwd / ".git").exists():
                settings["git_commit"] = (
                    subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=str(git_cwd), stderr=subprocess.DEVNULL
                    )
                    .decode()
                    .strip()
                )
                break
            if git_cwd.parent == git_cwd:
                break
            git_cwd = git_cwd.parent
    except Exception:
        settings["git_commit"] = None

    atomic_write_json(paths["settings_snapshot"], {"written_at": now_iso(), **settings})
    launch_payload = {
        "launched_at": now_iso(),
        "pid": os.getpid(),
        "strategy_version": config.STRATEGY_VERSION,
        "mode": settings["mode_flag"],
        "run_id": run_id or None,
        "ttl_policy": config.TTL_POLICY,
        "hard_exclude": sorted(config.HARD_EXCLUDE_SYMBOLS),
        "pending": config.pending_confirmations(),
        "fee": config.FEE_RATE_ONE_SIDE,
        "slip": config.SLIPPAGE_ADVERSE,
        "fill_model": config.SIMULATION_ASSUMPTIONS,
        "settings_snapshot_path": str(paths["settings_snapshot"]),
        "native_quote_vol_path": str(paths["quote_vol"]),
        "settings": settings,
        "git_commit": settings.get("git_commit"),
        "observe_only": observe_only,
        "no_open_close": bool(observe_only),
        "no_exchange_orders": True if observe_only else None,
    }
    atomic_write_json(paths["launch"], launch_payload)
    if observe_only:
        atomic_write_json(
            paths["observation_meta"],
            {
                "written_at": now_iso(),
                "run_id": run_id,
                "mode": "OBSERVATION_ONLY",
                "settings_tag": "NEW_OBSERVATION_SETTINGS",
                "RANGE_AMP_MAX": config.RANGE_AMP_MAX,
                "BAR_AMP_MAX": config.BAR_AMP_MAX,
                "git_commit": settings.get("git_commit"),
                "strategy_version": config.STRATEGY_VERSION,
                "note": "shadow observational_touch is NOT a fill / NOT PnL",
            },
        )

    print(f"[{now_iso()}] D 入口", flush=True)
    print(f"版本：{config.STRATEGY_VERSION}", flush=True)
    print(f"資料根：{config.RUNTIME_ROOT}", flush=True)
    print(f"模式：{settings['mode_flag']}", flush=True)
    if observe_only:
        print("說明：OBSERVATION_ONLY；不掛交易所單、不產生 paper OPEN/CLOSE；影子觸價非成交。", flush=True)
    else:
        print("說明：模擬；不會呼叫實盤下單 API。結果非實盤成果。", flush=True)

    replay_sess = None
    if not observe_only:
        try:
            from common.replay_archive.strategy_bridge import maybe_start_from_config_module

            replay_sess = maybe_start_from_config_module(
                config,
                testnet_cli_flag=bool(getattr(args, "testnet", False)),
                strategy_id_override=getattr(config, "PROFILE_ID", None) or config.STRATEGY_ID,
            )
            if replay_sess:
                print(
                    f"[replay_archive] enabled mode={replay_sess.execution_mode} "
                    f"session={replay_sess.session_id}",
                    flush=True,
                )
        except Exception as _replay_exc:  # noqa: BLE001
            print(f"[replay_archive] start skipped: {_replay_exc}", flush=True)

    def _stop(signum, frame):  # noqa: ARG001
        print(f"\n[停止] 收到 {signum}，保存後退出（不平殺）", flush=True)
        _release_lock()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        if observe_only:
            asyncio.run(
                run_observe_loop(
                    hours=args.hours,
                    once=args.once,
                    run_id=run_id,
                    max_events=int(args.max_events or 0),
                    disk_limit_mb=float(args.disk_limit_mb or 512.0),
                )
            )
        else:
            asyncio.run(run_paper_loop(hours=args.hours, once=args.once, allow_fills=allow_fills))
    finally:
        try:
            if replay_sess is not None:
                replay_sess.close()
        except Exception:
            pass
        _release_lock()


if __name__ == "__main__":
    main()
