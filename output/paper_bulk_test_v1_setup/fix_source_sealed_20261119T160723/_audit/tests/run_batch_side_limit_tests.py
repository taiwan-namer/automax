#!/usr/bin/env python3
"""同掃描批次同向最多一檔成功新開 — 離線驗收（隔離帳戶副本）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, Position, save_account
from breakout_follow_v1.engine import (
    SCAN_BATCH_SIDE_OPEN_LIMIT,
    _candidate_rank_key,
    execute_reverse,
)
from breakout_follow_v1.portfolio import try_open
from breakout_follow_v1.util import now_iso, today_key

PASS = FAIL = 0
RESULTS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append(f"PASS {name}")
        print(f"PASS {name}")
    else:
        FAIL += 1
        RESULTS.append(f"FAIL {name} {detail}")
        print(f"FAIL {name} {detail}")


def _sig(symbol: str, side: str, vol_x: float, spread: float, sot: int, entry: float = 100.0):
    return {
        "side": side,
        "est_fill_price": entry,
        "atr": 0.4,
        "atr_time": now_iso(),
        "signal_open_time": sot,
        "range_high": entry + 1.0 if side == "LONG" else entry - 0.1,
        "range_low": entry - 1.0 if side == "LONG" else entry + 0.1,
        "vol_x": vol_x,
        "spread": spread,
    }


def _filt():
    return {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5}


def _apply_open_loop(candidates: list[dict], st: AccountState) -> dict[str, Any]:
    """複現 scan_once 開倉迴圈的同批同向上限語意（不含網路）。"""
    ranked = sorted(candidates, key=_candidate_rank_key)
    batch_opened_by_side: dict[str, dict] = {}
    outcomes = []
    for c in ranked:
        sym = c["symbol"]
        side = c["side"]
        if sym in st.positions:
            outcomes.append({"symbol": sym, "action": "SKIP_HELD"})
            continue
        slot = batch_opened_by_side.get(side)
        if slot is not None and len(slot.get("trade_ids") or []) >= SCAN_BATCH_SIDE_OPEN_LIMIT:
            outcomes.append(
                {
                    "symbol": sym,
                    "side": side,
                    "ok": False,
                    "block_reason": "SAME_SCAN_BATCH_SIDE_LIMIT",
                    "winner": slot.get("symbol"),
                }
            )
            continue
        opened = try_open(
            st,
            symbol=sym,
            side=side,
            signal=c["signal"],
            source_tags=c.get("tags") or ["T"],
            filters=_filt(),
            linked_prev=c.get("linked_prev"),
            bypass_cooldown=bool(c.get("bypass_cooldown")),
        )
        if opened.get("ok"):
            batch_opened_by_side[side] = {
                "symbol": sym,
                "trade_ids": [opened.get("trade_id")],
                "vol_x": c["vol_x"],
                "spread": c["spread"],
            }
        outcomes.append(
            {
                "symbol": sym,
                "side": side,
                "ok": bool(opened.get("ok")),
                "block_reason": opened.get("block_reason"),
                "trade_id": opened.get("trade_id"),
            }
        )
    return {"ranked": ranked, "opened_by_side": batch_opened_by_side, "outcomes": outcomes}


def main() -> int:
    check("limit_is_one", SCAN_BATCH_SIDE_OPEN_LIMIT == 1)

    # 同分可重現排序
    cands = [
        {"symbol": "BUSDT", "vol_x": 3.0, "spread": 0.02, "side": "LONG"},
        {"symbol": "AUSDT", "vol_x": 5.0, "spread": 0.05, "side": "LONG"},
        {"symbol": "CUSDT", "vol_x": 5.0, "spread": 0.01, "side": "LONG"},
        {"symbol": "DUSDT", "vol_x": 5.0, "spread": 0.01, "side": "LONG"},
    ]
    r1 = sorted(cands, key=_candidate_rank_key)
    r2 = sorted(list(reversed(cands)), key=_candidate_rank_key)
    order = [c["symbol"] for c in r1]
    check("rank_repro", order == [c["symbol"] for c in r2])
    check("rank_vol_then_spread_then_symbol", order == ["CUSDT", "DUSDT", "AUSDT", "BUSDT"])

    td = Path(tempfile.mkdtemp(prefix="batchside_"))
    config.apply_profile("paper_bulk_test_v1")
    config.ACCOUNT_PATH = td / "account_state.json"
    config.ROOT_OUT = td
    config.LEDGER_PATH = td / "ledger.jsonl"
    config.FILLS_PATH = td / "fills.jsonl"
    config.DECISION_TRACE_PATH = td / "decisions.jsonl"
    (td / "decision_snapshots").mkdir(parents=True, exist_ok=True)

    st = AccountState(
        run_id="batch",
        cash_u=50_000,
        initial_capital_u=50_000,
        day_key=today_key(),
        day_start_equity_u=50_000.0,
    )
    candidates = []
    for i, (sym, vx, sp) in enumerate(
        [("ZUSDT", 2.0, 0.05), ("YUSDT", 9.0, 0.02), ("XUSDT", 9.0, 0.01), ("WUSDT", 4.0, 0.03)]
    ):
        sig = _sig(sym, "LONG", vx, sp, sot=100 + i)
        candidates.append(
            {
                "symbol": sym,
                "side": "LONG",
                "signal": sig,
                "tags": ["T"],
                "vol_x": vx,
                "spread": sp,
                "sig_key": f"{sym}:{100+i}:LONG",
            }
        )
    # 另加 SHORT 一檔，應可與 LONG 各開一檔
    sig_s = _sig("SUSDT", "SHORT", 8.0, 0.01, sot=200, entry=100.0)
    candidates.append(
        {
            "symbol": "SUSDT",
            "side": "SHORT",
            "signal": sig_s,
            "tags": ["T"],
            "vol_x": 8.0,
            "spread": 0.01,
            "sig_key": "SUSDT:200:SHORT",
        }
    )

    res = _apply_open_loop(candidates, st)
    long_ok = [o for o in res["outcomes"] if o.get("side") == "LONG" and o.get("ok")]
    long_blocked = [
        o for o in res["outcomes"] if o.get("side") == "LONG" and o.get("block_reason") == "SAME_SCAN_BATCH_SIDE_LIMIT"
    ]
    short_ok = [o for o in res["outcomes"] if o.get("side") == "SHORT" and o.get("ok")]
    check("same_batch_long_at_most_one", len(long_ok) == 1)
    check("same_batch_long_winner_is_best", long_ok[0]["symbol"] == "XUSDT")  # vol 9, spread 0.01
    check("same_batch_long_others_limited", len(long_blocked) >= 1)
    check("same_batch_short_independent", len(short_ok) == 1)

    # 勝者已持倉：同批候選再跑不得重複開該標的
    winner_sym = long_ok[0]["symbol"]
    check("winner_held", winner_sym in st.positions)
    res2 = _apply_open_loop(
        [c for c in candidates if c["symbol"] == winner_sym],
        st,
    )
    long_ok2 = [o for o in res2["outcomes"] if o.get("side") == "LONG" and o.get("ok")]
    check("no_repeat_open_held_winner", len(long_ok2) == 0 and res2["outcomes"][0].get("action") == "SKIP_HELD")

    # 反手：退出必須執行；新開走 pending_reopen（不繞過）
    st_r = AccountState(
        run_id="rev",
        cash_u=50_000,
        initial_capital_u=50_000,
        day_key=today_key(),
        day_start_equity_u=50_000.0,
    )
    # seed LONG
    seed = try_open(
        st_r,
        symbol="RUSDT",
        side="LONG",
        signal=_sig("RUSDT", "LONG", 5.0, 0.01, sot=1),
        source_tags=["T"],
        filters=_filt(),
    )
    check("reverse_seed_open", seed.get("ok") is True)
    book = {"bid": 99.0, "ask": 99.1, "fetched_at": __import__("time").time(), "mid": 99.05, "spread": 0.1}
    short_sig = _sig("RUSDT", "SHORT", 6.0, 0.01, sot=2, entry=99.0)
    with patch("breakout_follow_v1.engine.time.time", return_value=book["fetched_at"]):
        rev = execute_reverse(
            st_r,
            symbol="RUSDT",
            sig=short_sig,
            book=book,
            source_tags=["T"],
            allow_reopen=False,
        )
    check("reverse_close_always", rev.get("closed") is True)
    check("reverse_no_immediate_open", rev.get("opened") is False)
    check("reverse_pending_present", isinstance(rev.get("pending_reopen"), dict))
    check("reverse_position_cleared", "RUSDT" not in st_r.positions)

    # 同批已有 SHORT 贏家時，反手 pending 應被上限擋下，但不回補舊倉
    st_r2 = AccountState(
        run_id="rev2",
        cash_u=50_000,
        initial_capital_u=50_000,
        day_key=today_key(),
        day_start_equity_u=50_000.0,
    )
    try_open(
        st_r2,
        symbol="HOLDUSDT",
        side="LONG",
        signal=_sig("HOLDUSDT", "LONG", 1.0, 0.05, sot=10),
        source_tags=["T"],
        filters=_filt(),
    )
    book2 = {"bid": 100.0, "ask": 100.1, "fetched_at": __import__("time").time(), "mid": 100.05, "spread": 0.1}
    with patch("breakout_follow_v1.engine.time.time", return_value=book2["fetched_at"]):
        rev2 = execute_reverse(
            st_r2,
            symbol="HOLDUSDT",
            sig=_sig("HOLDUSDT", "SHORT", 3.0, 0.02, sot=11, entry=100.0),
            book=book2,
            source_tags=["T"],
            allow_reopen=False,
        )
    pending = rev2["pending_reopen"]
    # 先開另一 SHORT 佔用同向名額（評分更高）
    other = {
        "symbol": "WINUSDT",
        "side": "SHORT",
        "signal": _sig("WINUSDT", "SHORT", 9.0, 0.01, sot=12, entry=100.0),
        "tags": ["T"],
        "vol_x": 9.0,
        "spread": 0.01,
        "sig_key": "WINUSDT:12:SHORT",
    }
    pend_cand = {
        "symbol": pending["symbol"],
        "side": pending["side"],
        "signal": pending["signal"],
        "tags": pending["source_tags"],
        "vol_x": pending["vol_x"],
        "spread": pending["spread"],
        "sig_key": pending["sig_key"],
        "linked_prev": pending["linked_prev"],
        "bypass_cooldown": True,
        "is_reverse_reopen": True,
    }
    res_r = _apply_open_loop([other, pend_cand], st_r2)
    short_ok_r = [o for o in res_r["outcomes"] if o.get("side") == "SHORT" and o.get("ok")]
    short_lim = [
        o
        for o in res_r["outcomes"]
        if o.get("symbol") == "HOLDUSDT" and o.get("block_reason") == "SAME_SCAN_BATCH_SIDE_LIMIT"
    ]
    check("reverse_reopen_respects_batch_limit", len(short_ok_r) == 1 and short_ok_r[0]["symbol"] == "WINUSDT")
    check("reverse_reopen_blocked_not_bypass", len(short_lim) == 1)
    check("reverse_exit_not_undone", "HOLDUSDT" not in st_r2.positions)

    print(f"\npassed={PASS} failed={FAIL}")
    out = ROOT / "BATCH_SIDE_LIMIT_TEST_RESULTS.md"
    out.write_text(
        "# 同批同向上限離線測試\n\n"
        f"- passed: {PASS}\n- failed: {FAIL}\n\n"
        + "\n".join(f"- {r}" for r in RESULTS)
        + "\n",
        encoding="utf-8",
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
