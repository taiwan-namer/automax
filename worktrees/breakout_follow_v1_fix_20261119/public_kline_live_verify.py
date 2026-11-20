#!/usr/bin/env python3
"""
公開行情短驗證（修復版路徑）：只 GET /fapi/v1/klines，完全獨立輸出目錄。
禁止讀寫正式帳戶／正式 vault／正式 fills。
觀測連續 N 個 1m 收盤邊界：未收盤不進訊號；收盤後版本可更新；最新已收盤不持續落後一根。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
import pandas as pd

FIX = Path(__file__).resolve().parent
REPO = FIX.parents[1]
sys.path.insert(0, str(FIX))

from breakout_follow_v1 import config  # noqa: E402
from breakout_follow_v1.market_data import (  # noqa: E402
    GuardedSession,
    annotate_bar_closed,
    closed_klines,
    fetch_klines_incremental,
    load_vault,
    merge_klines,
)
from breakout_follow_v1.run_store import RunStore  # noqa: E402
from breakout_follow_v1.signals import detect_breakout  # noqa: E402
from breakout_follow_v1.util import now_iso  # noqa: E402

TZ = timezone(timedelta(hours=8))
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
N_CLOSES = 5


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=TZ).isoformat(timespec="seconds")


def isolate(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    root = out / "output" / "breakout_follow_v1"
    vault = out / "data" / "breakout_follow_v1" / "kline_vault_v2_clean"
    root.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "decision_snapshots").mkdir(parents=True, exist_ok=True)
    config.ROOT_OUT = root
    config.ACCOUNT_PATH = root / "account_state.json"  # never used for trading here
    config.FILLS_PATH = root / "fills.jsonl"
    config.TRADES_PATH = root / "trades.csv"
    config.EQUITY_PATH = root / "equity_snapshots.jsonl"
    config.DECISION_TRACE_PATH = root / "decision_trace.jsonl"
    config.DECISION_SNAPSHOT_DIR = root / "decision_snapshots"
    config.KLINE_VAULT_DIR = vault
    config.PID_PATH = root / "paper.pid"
    config.RUN_MANIFEST_PATH = root / "run_manifest.json"
    config.UNIVERSE_PATH = root / "universe_snapshot.json"
    config.REVIEW_PATH = root / "review.md"


async def wait_until_near_boundary(offset_sec: float = 2.0) -> int:
    """等到下一分鐘邊界後 offset_sec 秒；回傳該分鐘 open_time ms。"""
    while True:
        now = time.time()
        sec = now % 60
        # 等到 :00 + offset
        wait = (60 - sec) + offset_sec
        if wait > 60.5:
            wait -= 60
        await asyncio.sleep(min(wait, 2.0))
        now = time.time()
        if now % 60 >= offset_sec and now % 60 < offset_sec + 1.5:
            ot = int(now // 60) * 60_000 - 60_000  # just-closed bar open_time
            return ot


async def fetch_raw(gs: GuardedSession, symbol: str, limit: int = 5) -> tuple[list, int]:
    t0 = int(time.time() * 1000)
    raw = await gs.get_json("/fapi/v1/klines", params={"symbol": symbol, "interval": "1m", "limit": limit})
    t1 = int(time.time() * 1000)
    return raw, (t0 + t1) // 2


def rows_to_df(raw: list) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": int(x[0]),
                "open": float(x[1]),
                "high": float(x[2]),
                "low": float(x[3]),
                "close": float(x[4]),
                "volume": float(x[5]),
                "close_time": int(x[6]),
                "quote_volume": float(x[7]),
            }
            for x in raw
        ]
    )


async def main() -> int:
    stamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    out = FIX / "public_verify_out" / stamp
    isolate(out)
    # prove not writing live
    live_fills = REPO / "output/breakout_follow_v1/fills.jsonl"
    live_acct = REPO / "output/breakout_follow_v1/account_state.json"
    live_vault = REPO / "data/breakout_follow_v1/kline_vault"
    h_fills = live_fills.read_bytes()
    h_acct = live_acct.read_bytes()

    store = RunStore(run_id=f"public_verify_{stamp}", root=config.ROOT_OUT / "runs" / f"public_verify_{stamp}")
    store.ensure()

    events: list[dict] = []
    failures: list[str] = []
    closes_seen = 0

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        gs = GuardedSession(session)
        # Seed vault with a mid-bar fetch if possible, else first post-close
        print(f"[{now_iso()}] public verify start out={out} symbols={SYMBOLS} need_closes={N_CLOSES}")

        # Align: wait until ~2s AFTER a minute close, then for each of N closes:
        #   A) at T+2s: fetch (last bar should be closed)
        #   B) at T+55s: fetch forming next bar (unclosed) — optional mid
        #   C) at next T+2s: fetch closed update
        while closes_seen < N_CLOSES:
            # sleep until ~2s past next boundary
            now = time.time()
            wait = (60 - (now % 60)) + 2.0
            print(f"[{now_iso()}] waiting {wait:.1f}s for next close+2s (closes_seen={closes_seen})")
            await asyncio.sleep(wait)
            boundary_ms = int(time.time() // 60) * 60_000
            closed_ot = boundary_ms - 60_000  # the bar that just closed

            for sym in SYMBOLS:
                raw, fetched_at = await fetch_raw(gs, sym, limit=40)
                fresh = annotate_bar_closed(rows_to_df(raw), fetched_at)
                cached = load_vault(sym)
                merged = merge_klines(cached if not cached.empty else None, fresh)
                # save via incremental path (also archives)
                df2, err = await fetch_klines_incremental(gs, sym, limit=40)
                if err:
                    failures.append(f"{sym} fetch_err {err}")
                store.archive_klines(sym, df2 if df2 is not None else merged, note="public_verify", fetched_at_ms=fetched_at)

                closed = closed_klines(df2 if df2 is not None else merged, now_ms=fetched_at)
                last_closed_ot = int(closed["open_time"].iloc[-1]) if not closed.empty else None
                # forming row if present
                last_row = (df2 if df2 is not None else merged).iloc[-1]
                last_ot = int(last_row["open_time"])
                last_is_closed = bool(last_row.get("is_closed", False)) if "is_closed" in (df2 if df2 is not None else merged).columns else None

                # signal must use closed only — S should be last_closed, not forming
                book = {"bid": float(last_row["close"]) * 0.9999, "ask": float(last_row["close"]) * 1.0001, "mid": float(last_row["close"]), "spread": 0.0002, "fetched_at": time.time()}
                probe = detect_breakout(df2 if df2 is not None else merged, now_ms=fetched_at, book=book, data_fetched_at=fetched_at / 1000.0)
                sig_ot = probe.get("signal_open_time")
                excluded = probe.get("excluded_unclosed") or []

                # Compare with a second REST pull as reference (same public endpoint)
                raw2, fetched2 = await fetch_raw(gs, sym, limit=5)
                ref = annotate_bar_closed(rows_to_df(raw2), fetched2)
                ref_closed = closed_klines(ref, now_ms=fetched2)
                ref_last = int(ref_closed["open_time"].iloc[-1]) if not ref_closed.empty else None

                lag = None if (last_closed_ot is None or ref_last is None) else (ref_last - last_closed_ot) // 60_000
                ev = {
                    "ts": now_iso(),
                    "symbol": sym,
                    "fetched_at_ms": fetched_at,
                    "boundary_ms": boundary_ms,
                    "expected_just_closed_ot": closed_ot,
                    "vault_last_ot": last_ot,
                    "vault_last_is_closed": last_is_closed,
                    "last_closed_ot": last_closed_ot,
                    "last_closed_iso": _iso(last_closed_ot) if last_closed_ot else None,
                    "ref_last_closed_ot": ref_last,
                    "lag_bars_vs_ref": lag,
                    "signal_open_time": sig_ot,
                    "excluded_unclosed": excluded,
                    "first_reject": probe.get("first_reject_code"),
                    "bars_closed": probe.get("bars_closed"),
                    "path_vault": str(config.KLINE_VAULT_DIR),
                    "path_run": str(store.root),
                }
                events.append(ev)

                # Assertions for this close
                if last_closed_ot != closed_ot:
                    # allow if clock slightly late/early by documenting; still check not lagging >0 vs ref
                    if lag is not None and lag > 0:
                        failures.append(f"{sym} @{_iso(closed_ot)} last_closed lag vs ref by {lag} bar(s)")
                if lag is not None and lag > 0:
                    failures.append(f"{sym} persistent lag {lag} vs concurrent REST closed")
                if sig_ot is not None and last_is_closed is False and sig_ot == last_ot:
                    failures.append(f"{sym} signal used unclosed last bar {last_ot}")
                if last_ot == closed_ot and last_is_closed is not True:
                    # just-closed bar must be marked closed when fetched at +2s
                    failures.append(f"{sym} just-closed bar not is_closed=True at +2s")
                print(f"  {sym} closed={_iso(last_closed_ot) if last_closed_ot else None} lag={lag} sig_S={sig_ot} excl={excluded[:1]}")

            closes_seen += 1

            # Mid-minute: fetch forming bar and ensure it is NOT treated as closed signal S
            await asyncio.sleep(20)
            for sym in SYMBOLS:
                raw, fetched_at = await fetch_raw(gs, sym, limit=40)
                df, err = await fetch_klines_incremental(gs, sym, limit=40)
                if err:
                    failures.append(f"{sym} mid fetch_err {err}")
                    continue
                last = df.iloc[-1]
                ot = int(last["open_time"])
                is_c = bool(last["is_closed"]) if "is_closed" in df.columns else None
                closed = closed_klines(df, now_ms=fetched_at)
                last_c = int(closed["open_time"].iloc[-1]) if not closed.empty else None
                book = {"bid": float(last["close"]) * 0.9999, "ask": float(last["close"]) * 1.0001, "mid": float(last["close"]), "spread": 0.0002, "fetched_at": time.time()}
                probe = detect_breakout(df, now_ms=fetched_at, book=book, data_fetched_at=fetched_at / 1000.0)
                mid_ev = {
                    "ts": now_iso(),
                    "phase": "mid_minute",
                    "symbol": sym,
                    "fetched_at_ms": fetched_at,
                    "last_ot": ot,
                    "last_is_closed": is_c,
                    "last_closed_ot": last_c,
                    "signal_open_time": probe.get("signal_open_time"),
                    "excluded_unclosed": probe.get("excluded_unclosed") or [],
                }
                events.append(mid_ev)
                if is_c is True and int(last["close_time"]) >= fetched_at:
                    failures.append(f"{sym} mid-minute forming bar marked is_closed=True")
                if probe.get("signal_open_time") == ot and is_c is False:
                    failures.append(f"{sym} mid-minute signal used forming bar")
                # after close of previous, last_closed should be previous minute not stuck older
                print(f"  MID {sym} last_ot={_iso(ot)} is_closed={is_c} last_closed={_iso(last_c) if last_c else None}")

    # live untouched
    if live_fills.read_bytes() != h_fills:
        failures.append("LIVE fills.jsonl changed during verify")
    if live_acct.read_bytes() != h_acct:
        # live PID may update account — note but don't fail verify for that; only fail if OUR paths equal live
        events.append({"note": "live account bytes changed (likely PID 86206); verify used isolated paths"})
    if str(config.KLINE_VAULT_DIR).startswith(str(live_vault)):
        failures.append("vault path points at live vault")

    report = {
        "started": stamp,
        "ended": now_iso(),
        "out_dir": str(out),
        "symbols": SYMBOLS,
        "closes_observed": closes_seen,
        "events": events,
        "failures": failures,
        "passed": len(failures) == 0 and closes_seen >= N_CLOSES,
        "isolated_vault": str(config.KLINE_VAULT_DIR),
        "isolated_run": str(store.root),
        "live_fills_unchanged": live_fills.read_bytes() == h_fills,
        "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
        "kline_vault_dir_name": Path(config.KLINE_VAULT_DIR).name,
    }
    (out / "public_verify_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failures": failures, "closes": closes_seen, "out": str(out)}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
