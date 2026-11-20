"""SPIKE_FADE_1M 全面禁空（fade_long_only_v2）回歸。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from live_paper_trader import (
    PaperState,
    SCHOOL_SPIKE_FADE,
    SCHOOL_SPIKE_MOMO,
    detect_spike_fade_1m,
    open_position,
)
from signal_reject_diag import begin_probe, take_probe


def _fade_short_df() -> pd.DataFrame:
    """T-1 爆量紅K上影 + T 收黑（修補前會出 SHORT）。"""
    n = config.VOLUME_MA_PERIOD + 5
    end = int(time.time() * 1000) // 60_000 * 60_000 - 60_000
    rows = []
    for i in range(n):
        ot = end - (n - 1 - i) * 60_000
        if i == n - 2:
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.0,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 100.5,
                    "volume": 1000.0,
                }
            )
        elif i == n - 1:
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.5,
                    "high": 100.6,
                    "low": 99.5,
                    "close": 99.6,
                    "volume": 120.0,
                }
            )
        else:
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.0,
                    "high": 100.2,
                    "low": 99.8,
                    "close": 100.05,
                    "volume": 100.0,
                }
            )
    return pd.DataFrame(rows)


def _fade_long_df() -> pd.DataFrame:
    """T-1 爆量黑K下影 + T 收紅。"""
    n = config.VOLUME_MA_PERIOD + 5
    end = int(time.time() * 1000) // 60_000 * 60_000 - 60_000
    rows = []
    for i in range(n):
        ot = end - (n - 1 - i) * 60_000
        if i == n - 2:
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.0,
                    "high": 100.0,
                    "low": 98.0,
                    "close": 99.5,
                    "volume": 1000.0,
                }
            )
        elif i == n - 1:
            rows.append(
                {
                    "open_time": ot,
                    "open": 99.5,
                    "high": 100.5,
                    "low": 99.4,
                    "close": 100.4,
                    "volume": 120.0,
                }
            )
        else:
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.0,
                    "high": 100.2,
                    "low": 99.8,
                    "close": 100.05,
                    "volume": 100.0,
                }
            )
    return pd.DataFrame(rows)


def test_mild_short_pattern_now_rejected():
    df = _fade_short_df()
    ctx = {"price_position_pct": 50.0, "price_change_pct_24h": 1.0}
    begin_probe(school=SCHOOL_SPIKE_FADE, symbol="T", ctx=ctx)
    sig = detect_spike_fade_1m(df, ctx=ctx)
    probe = take_probe()
    assert sig is None
    assert probe["first_reject_code"] == "FADE_SHORT_DISABLED"


def test_strong_short_pattern_also_rejected():
    df = _fade_short_df()
    ctx = {"price_position_pct": 85.0, "price_change_pct_24h": 30.0}
    begin_probe(school=SCHOOL_SPIKE_FADE, symbol="T", ctx=ctx)
    sig = detect_spike_fade_1m(df, ctx=ctx)
    probe = take_probe()
    assert sig is None
    assert probe["first_reject_code"] == "FADE_SHORT_DISABLED"


def test_long_signal_stable():
    df = _fade_long_df()
    ctx = {"price_position_pct": 40.0, "price_change_pct_24h": 5.0}
    a = detect_spike_fade_1m(df, ctx=ctx)
    begin_probe(school=SCHOOL_SPIKE_FADE, symbol="T", ctx=ctx)
    b = detect_spike_fade_1m(df, ctx=ctx)
    probe = take_probe()
    assert a is not None and b is not None
    assert a["side"] == b["side"] == "LONG"
    assert abs(a["entry"] - b["entry"]) < 1e-12
    assert a["signal_key"] == b["signal_key"]
    assert probe["result"] == "SIGNAL"
    assert probe["first_reject_code"] is None


def test_open_position_blocks_fade_short_without_side_effects():
    audit = Path("output") / f"trade_audit_{__import__('datetime').datetime.now().astimezone().strftime('%Y%m%d')}.jsonl"
    before_audit = audit.read_text(encoding="utf-8") if audit.exists() else None
    state = PaperState()
    before_bal = state.balance
    before_realized = state.realized_net_pnl
    before_pos = dict(state.positions)
    try:
        opened = open_position(
            state,
            symbol="FADETEST",
            school=SCHOOL_SPIKE_FADE,
            side="SHORT",
            entry=100.0,
            params={"tp_pct": 0.85, "sl_pct": 0.65, "max_hold_mins": 12},
            reason="forced FADE SHORT",
            mode="once",
        )
        assert opened is None
        assert state.balance == before_bal
        assert state.realized_net_pnl == before_realized
        assert state.positions == before_pos
        assert "FADETEST" not in state.positions
    finally:
        if before_audit is None:
            if audit.exists():
                audit.unlink()
        else:
            audit.write_text(before_audit, encoding="utf-8")


def test_other_school_short_not_blocked_by_fade_gate(tmp_path=None):
    """確認 FADE 閘門不擋其他門派 SHORT；並清掉測試寫入的帳本殘留。"""
    import live_paper_trader as lpt

    log_path = Path(getattr(config, "TRADES_LOG_CSV", "output/live_trades_log.csv"))
    before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    state = PaperState()
    try:
        opened = open_position(
            state,
            symbol="MOMOTEST",
            school=SCHOOL_SPIKE_MOMO,
            side="SHORT",
            entry=100.0,
            params={"tp_pct": 1.8, "sl_pct": 0.85, "max_hold_mins": 20},
            reason="momo short ok",
            mode="once",
        )
        assert opened is not None
        assert opened.side == "SHORT"
        assert "MOMOTEST" in state.positions
    finally:
        # 還原正式帳本，避免測試污染歷史
        if log_path.exists():
            log_path.write_text(before, encoding="utf-8")
        # 從記憶體移除
        state.positions.pop("MOMOTEST", None)


def test_fade_t1_t_binding_unchanged_on_long():
    """LONG 路徑仍：量倍／影線在 T-1，entry 在 T close。"""
    df = _fade_long_df()
    ctx = {"price_position_pct": 40.0, "price_change_pct_24h": 5.0}
    begin_probe(school=SCHOOL_SPIKE_FADE, symbol="T", ctx=ctx)
    sig = detect_spike_fade_1m(df, ctx=ctx)
    probe = take_probe()
    assert sig is not None and sig["side"] == "LONG"
    # signal_bar in probe is T-1 (spike)
    assert probe.get("signal_bar_open_time") == int(df["open_time"].iloc[-2])
    assert abs(sig["entry"] - float(df["close"].iloc[-1])) < 1e-12
    vm = probe.get("vol_metric") or {}
    assert vm.get("exclude_signal_bar_from_denominator") is True
    assert int(vm.get("numerator_open_time")) == int(df["open_time"].iloc[-2])


if __name__ == "__main__":
    test_mild_short_pattern_now_rejected()
    test_strong_short_pattern_also_rejected()
    test_long_signal_stable()
    test_open_position_blocks_fade_short_without_side_effects()
    test_other_school_short_not_blocked_by_fade_gate()
    test_fade_t1_t_binding_unchanged_on_long()
    print("fade_long_only ok")
