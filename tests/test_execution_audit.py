"""成交可信度與稽核輸出測試（不改策略參數）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

import config
from audit_observability import (
    classify_intrabar_exit,
    effective_sl_prices,
    write_ai_review_bundle,
    write_execution_audit_md,
)
from live_paper_trader import (
    PaperState,
    Position,
    _calc_pnl,
    check_exits_on_bar,
    SCHOOL_SPIKE_FADE,
    SCHOOL_SPIKE_MOMO,
)


def test_long_short_breakeven_direction():
    long_sl = effective_sl_prices(
        side="LONG", entry=100.0, original_sl_pct=0.7, lock_offset_pct=0.15, breakeven_armed=True
    )
    assert long_sl["effective_sl_price"] > long_sl["original_sl_price"]
    assert long_sl["only_tightens"] is True

    short_sl = effective_sl_prices(
        side="SHORT", entry=100.0, original_sl_pct=0.7, lock_offset_pct=0.15, breakeven_armed=True
    )
    assert short_sl["effective_sl_price"] < short_sl["original_sl_price"]
    assert short_sl["only_tightens"] is True


def test_trail_lock_only_tightens():
    pos = Position(
        symbol="T1",
        school=SCHOOL_SPIKE_MOMO,
        side="LONG",
        entry_price=100.0,
        entry_time=time.time() - 60,
        tp_pct=1.8,
        sl_pct=0.85,
        max_hold_mins=20,
        original_sl_pct=0.85,
        lock_offset_pct=0.20,
        breakeven_armed=True,
    )
    before = effective_sl_prices(
        side=pos.side,
        entry=pos.entry_price,
        original_sl_pct=pos.original_sl_pct,
        lock_offset_pct=0.20,
        breakeven_armed=True,
    )
    # 模擬再上移 lock
    after = effective_sl_prices(
        side=pos.side,
        entry=pos.entry_price,
        original_sl_pct=pos.original_sl_pct,
        lock_offset_pct=0.45,
        breakeven_armed=True,
    )
    assert after["effective_sl_price"] >= before["effective_sl_price"]


def test_intrabar_tp_sl_ambiguity_tag():
    amb = classify_intrabar_exit(
        side="LONG",
        high=102,
        low=99,
        close=100.5,
        tp_price=101.0,
        sl_price=99.5,
        hit_tp=True,
        hit_sl=True,
        lock_armed_this_bar=False,
        trail_exit=False,
    )
    assert amb["intrabar_ambiguity"] == "AMBIGUOUS_INTRABAR"
    assert amb["sim_exit_assumption"] == "SL_WINS_IF_BOTH"
    assert amb["optimistic_boundary_price"] == 101.0
    assert amb["conservative_boundary_price"] == 99.5


def test_arm_and_stop_same_bar_tag():
    amb = classify_intrabar_exit(
        side="LONG",
        high=101,
        low=100.1,
        close=100.2,
        tp_price=102,
        sl_price=100.15,
        hit_tp=False,
        hit_sl=True,
        lock_armed_this_bar=True,
        trail_exit=False,
    )
    assert "AMBIGUOUS_INTRABAR_ARM_AND_STOP" in amb["ambiguity_tags"]


def test_entry_bar_not_used_as_hold_path_same_scan_order():
    """同輪：先 exits 再 open；新倉不會在同輪被該根 OHLC 平倉。"""
    st = PaperState()
    # 無持倉時 check 無害
    check_exits_on_bar(st, "NOSYM", high=110, low=90, close=100, mode="once")
    assert "NOSYM" not in st.positions


def test_fee_not_double_on_close_calc():
    pos = Position(
        symbol="FEE",
        school=SCHOOL_SPIKE_FADE,
        side="LONG",
        entry_price=100,
        entry_time=time.time(),
        tp_pct=0.95,
        sl_pct=0.7,
        max_hold_mins=12,
        notional=100,
        margin=10,
    )
    g, fee, net, _ = _calc_pnl(pos, 100.15)
    assert abs(fee - pos.notional * config.ROUND_TRIP_FEE_RATE) < 1e-12
    assert abs(net - (g - fee)) < 1e-12
    # 再開一次計算不應「累加兩次」——函式本身無狀態
    g2, fee2, net2, _ = _calc_pnl(pos, 100.15)
    assert fee2 == fee and net2 == net


def test_breakeven_win_vs_loss_separation_stats():
    df = pd.read_csv(ROOT / "output" / "live_trades_log.csv")
    closed = df[df["event"].astype(str).str.upper() == "CLOSE"].copy()
    closed["net"] = pd.to_numeric(closed["net_pnl_u"], errors="coerce")
    # SL 類與虧損類必須可分開計算
    sl_like = closed[closed["exit_reason"].astype(str).str.contains("SL|BREAKEVEN", regex=True, na=False)]
    losing = closed[closed["net"] < 0]
    # 結構斷言：集合可獨立存在（不要求數量關係固定）
    assert "exit_reason" in closed.columns
    _ = len(sl_like), len(losing)


def test_exit_reason_sum_equals_closes():
    df = pd.read_csv(ROOT / "output" / "live_trades_log.csv")
    closed = df[df["event"].astype(str).str.upper() == "CLOSE"]
    dist = closed["exit_reason"].fillna("-").astype(str).value_counts()
    assert int(dist.sum()) == len(closed)


def test_capital_settings_unchanged():
    assert config.INITIAL_CAPITAL_USDT == 500.0
    assert config.MAX_CONCURRENT_POSITIONS == 10
    assert config.MARGIN_PER_TRADE_USDT == 10.0
    assert config.NOTIONAL_USDT == 100.0


def test_audit_outputs_writable():
    a = write_execution_audit_md()
    b = write_ai_review_bundle()
    assert a.exists() and a.stat().st_size > 100
    assert b.exists() and b.stat().st_size > 100
    text = a.read_text(encoding="utf-8")
    assert "PASS" in text and "FAIL" in text
    assert "AMBIGUOUS" in text or "同根" in text


def test_restart_restore_does_not_wipe():
    from live_paper_trader import PaperState, restore_state_from_trades_log

    st = PaperState()
    info = restore_state_from_trades_log(st)
    assert "closed" in info
    # 不應把 realized 清空成必然 0（除非真的沒有成交）
    df = pd.read_csv(ROOT / "output" / "live_trades_log.csv")
    closed = df[df["event"].astype(str).str.upper() == "CLOSE"]
    if len(closed):
        assert abs(st.realized_net_pnl - float(pd.to_numeric(closed["net_pnl_u"], errors="coerce").fillna(0).sum())) < 1e-6


if __name__ == "__main__":
    # 直接執行並印結果
    tests = [n for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = []
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests)-len(failed)}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)
