#!/usr/bin/env python3
"""Offline tests for C: mirror-inverse of B (fixed gross 50/75)."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState
from breakout_follow_v1.mirror import process_mirror_entries
from breakout_follow_v1.portfolio import try_open


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name}: {detail}")
    print("PASS", name)


def _tmp_runtime():
    td = tempfile.mkdtemp(prefix="bfv1_mirror_inv_")
    config.set_runtime_root(td)
    return Path(td)


def _state(cash=50000.0):
    return AccountState(
        run_id="test_run_c",
        profile_id=config.PROFILE_ID,
        cash_u=cash,
        initial_capital_u=cash,
        day_start_equity_u=cash,
        day_key="2099-01-01",
    )


def test_profile_c_flags_and_isolation():
    rt = _tmp_runtime()
    a = config.apply_profile("paper_reverse_fixed_exit_v1")
    path_a = a["ACCOUNT_PATH"]
    b = config.apply_profile("paper_reverse_fixed_usdt_50_75_v1")
    path_b = b["ACCOUNT_PATH"]
    c = config.apply_profile("paper_reverse_fixed_usdt_50_75_inv_v1")
    check("C_mirror_on", c["MIRROR_ONLY_ENTRIES"] is True)
    check("C_src_B", c["MIRROR_SOURCE_PROFILE"] == "paper_reverse_fixed_usdt_50_75_v1")
    check("C_gross_on", c["FIXED_GROSS_EXIT_ENABLED"] is True)
    check("C_tp50", c["FIXED_GROSS_TP_USDT"] == 50.0)
    check("C_sl75", c["FIXED_GROSS_SL_USDT"] == 75.0)
    check("C_capital", c["INITIAL_CAPITAL_USDT"] == 50000.0)
    check("C_maxpos", c["MAX_CONCURRENT_POSITIONS"] == 20)
    check("C_dl", c["DAILY_LOSS_LOCK_USDT"] == 8000.0)
    check("C_paths", "paper_reverse_fixed_usdt_50_75_inv_v1" in c["ROOT_OUT"])
    check("C_ne_A", c["ACCOUNT_PATH"] != path_a)
    check("C_ne_B", c["ACCOUNT_PATH"] != path_b)
    check("C_vault_iso", "paper_reverse_fixed_usdt_50_75_inv_v1" in c["KLINE_VAULT_DIR"])
    # A/B still load correctly after C
    a2 = config.apply_profile("paper_reverse_fixed_exit_v1")
    check("A_after_C_no_mirror", a2.get("MIRROR_ONLY_ENTRIES") is False)
    check("A_after_C_no_gross", a2["FIXED_GROSS_EXIT_ENABLED"] is False)
    b2 = config.apply_profile("paper_reverse_fixed_usdt_50_75_v1")
    check("B_after_C_no_mirror", b2.get("MIRROR_ONLY_ENTRIES") is False)
    check("B_after_C_gross", b2["FIXED_GROSS_EXIT_ENABLED"] is True)
    check("runtime_tmp", str(rt) in c["RUNTIME_ROOT"] or str(rt) in config.RUNTIME_ROOT)


def test_mirror_opens_opposite_side():
    rt = _tmp_runtime()
    config.apply_profile("paper_reverse_fixed_usdt_50_75_inv_v1")
    # seed source B OPEN under same runtime root
    src_root = Path(config.RUNTIME_ROOT) / "output" / "paper_reverse_fixed_usdt_50_75_v1"
    src_root.mkdir(parents=True, exist_ok=True)
    (src_root / "fills.jsonl").write_text(
        json.dumps(
            {
                "event": "OPEN",
                "trade_id": "src-t1",
                "symbol": "AAAUSDT",
                "side": "LONG",
                "recorded_at": "2099-01-01T12:00:00+08:00",
                "atr": 0.01,
                "signal_open_time": 1,
                "fee_u": 0.1,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (src_root / "account_state.json").write_text(
        json.dumps(
            {
                "filters": {
                    "AAAUSDT": {
                        "stepSize": 1.0,
                        "minQty": 1.0,
                        "minNotional": 5.0,
                        "tickSize": 0.0001,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # force ignore_before earlier so seeded open is eligible
    c_root = Path(config.ROOT_OUT)
    c_root.mkdir(parents=True, exist_ok=True)
    (c_root / "mirror_state.json").write_text(
        json.dumps(
            {
                "mirrored_source_trade_ids": [],
                "started_at": "2099-01-01T00:00:00+08:00",
                "ignore_source_opens_before": "2099-01-01T00:00:00+08:00",
            }
        ),
        encoding="utf-8",
    )

    st = _state()
    books = {"AAAUSDT": {"bid": 0.99, "ask": 1.01, "fetched_at": 0.0}}
    gs = object()

    async def _run():
        return await process_mirror_entries(gs, st, books=books, stats={})

    out = asyncio.get_event_loop().run_until_complete(_run())
    check("opened_one", out.get("opened") == 1, str(out))
    check("pos_short", "AAAUSDT" in st.positions)
    check("side_inverse", st.positions["AAAUSDT"]["side"] == "SHORT")
    pos = st.positions["AAAUSDT"]
    check("has_tp", float(pos["take_profit"]) < float(pos["entry_price"]))
    check("has_sl", float(pos["initial_stop"]) > float(pos["entry_price"]))
    # idempotent
    out2 = asyncio.get_event_loop().run_until_complete(_run())
    check("no_dup", out2.get("opened") == 0, str(out2))
    check("tmp_ok", str(rt) in str(config.RUNTIME_ROOT))


def test_c_try_open_geometry_matches_b():
    _tmp_runtime()
    config.apply_profile("paper_reverse_fixed_usdt_50_75_inv_v1")
    filters = {"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0, "tickSize": 0.0001}
    st = _state()
    sig = {
        "signal_open_time": 1,
        "est_fill_price": 1.0,
        "atr": 0.01,
        "atr_time": "t",
        "range_high": 1.01,
        "range_low": 0.99,
        "vol_x": 0.0,
    }
    opened = try_open(
        st,
        symbol="GEOUSDT",
        side="LONG",
        signal=sig,
        source_tags=["T"],
        filters=filters,
        bypass_cooldown=True,
    )
    check("geo_open", opened.get("ok") is True, str(opened))
    pos = st.positions["GEOUSDT"]
    q = float(pos["qty_initial"])
    entry = float(pos["entry_price"])
    g_tp = (float(pos["take_profit"]) - entry) * q
    g_sl = (float(pos["initial_stop"]) - entry) * q
    check("geo_tp50", abs(g_tp - 50) < 0.05, f"{g_tp}")
    check("geo_sl75", abs(g_sl - (-75)) < 0.05, f"{g_sl}")


def main():
    test_profile_c_flags_and_isolation()
    test_mirror_opens_opposite_side()
    test_c_try_open_geometry_matches_b()
    print("\nALL mirror-inv-B (C) tests passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print("FAIL", e)
        raise
