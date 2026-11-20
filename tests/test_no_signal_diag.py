"""NO_SIGNAL 子條件診斷：確認紀錄不影響決策。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from live_paper_trader import detect_spike_fade_1m, detect_spike_momo_1m
from signal_reject_diag import begin_probe, take_probe, warn_write_error


def _bars(
    n: int,
    *,
    end_open_ms: int | None = None,
    vol: float = 100.0,
    body: str = "flat",
    spike_vol: float | None = None,
    spike_at: int = -1,
    wick: str | None = None,
) -> pd.DataFrame:
    """合成已對齊 open_time 的 1m K。"""
    end_open_ms = end_open_ms or (int(time.time() * 1000) // 60_000 * 60_000 - 60_000)
    rows = []
    for i in range(n):
        ot = end_open_ms - (n - 1 - i) * 60_000
        o = 100.0
        if body == "bull":
            c = 101.0
            h, l = 101.2, 99.8
        elif body == "bear":
            c = 99.0
            h, l = 100.2, 98.8
        else:
            c = 100.0
            h, l = 100.1, 99.9
        v = vol
        if spike_vol is not None and i == (n + spike_at if spike_at < 0 else spike_at):
            v = spike_vol
            if wick == "upper":
                # 紅K 長上影：o=100,c=100.5,h=102,l=100 → upper大
                o, c, h, l = 100.0, 100.5, 102.0, 100.0
            elif wick == "lower":
                o, c, h, l = 100.0, 99.5, 100.0, 98.0
            elif body == "bull_strong":
                o, c, h, l = 100.0, 101.5, 101.6, 99.9  # body high
            elif body == "momo_bull":
                o, c, h, l = 100.0, 102.0, 102.1, 99.95
        rows.append(
            {
                "open_time": ot,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
        )
    return pd.DataFrame(rows)


def _momo_signal_df() -> pd.DataFrame:
    """構造會過 MOMO 的資料：末根爆量 10x + 實體>=65% 紅K。"""
    n = config.VOLUME_MA_PERIOD + 5
    end = int(time.time() * 1000) // 60_000 * 60_000 - 60_000
    rows = []
    for i in range(n):
        ot = end - (n - 1 - i) * 60_000
        if i == n - 1:
            # 強紅實體
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.0,
                    "high": 102.05,
                    "low": 99.95,
                    "close": 102.0,
                    "volume": 1000.0,
                }
            )
        else:
            rows.append(
                {
                    "open_time": ot,
                    "open": 100.0,
                    "high": 100.2,
                    "low": 99.8,
                    "close": 100.1,
                    "volume": 100.0,
                }
            )
    return pd.DataFrame(rows)


def _fade_signal_df() -> pd.DataFrame:
    """T-1 爆量紅K上影 + T 收黑。"""
    n = config.VOLUME_MA_PERIOD + 5
    end = int(time.time() * 1000) // 60_000 * 60_000 - 60_000
    rows = []
    for i in range(n):
        ot = end - (n - 1 - i) * 60_000
        if i == n - 2:
            # T-1：紅K + 上影 >=40%
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
            # T：收黑
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


def test_momo_signal_unchanged_with_probe():
    df = _momo_signal_df()
    ctx = {"price_position_pct": 80.0}
    a = detect_spike_momo_1m(df, ctx=ctx)
    begin_probe(school="SPIKE_MOMO_1M", symbol="T", ctx=ctx)
    b = detect_spike_momo_1m(df, ctx=ctx)
    probe = take_probe()
    assert a is not None and b is not None
    assert a["side"] == b["side"] == "LONG"
    assert abs(a["entry"] - b["entry"]) < 1e-12
    assert a["signal_key"] == b["signal_key"]
    assert probe is not None and probe["result"] == "SIGNAL"
    assert probe["first_reject_code"] is None


def test_momo_no_signal_vol_reject_stable():
    df = _momo_signal_df()
    # 降低末根量 → 無訊號
    df = df.copy()
    df.loc[df.index[-1], "volume"] = 100.0
    ctx = {"price_position_pct": 80.0}
    a = detect_spike_momo_1m(df, ctx=ctx)
    begin_probe(school="SPIKE_MOMO_1M", symbol="T", ctx=ctx)
    b = detect_spike_momo_1m(df, ctx=ctx)
    probe = take_probe()
    assert a is None and b is None
    assert probe["first_reject_code"] == "VOL_X_RANGE"
    # 後續不得標 FAIL
    statuses = {c["code"]: c["status"] for c in probe["conditions"]}
    assert statuses.get("BODY_RATIO") == "NOT_EVALUATED"
    assert statuses.get("DIRECTION_POS_GATE") == "NOT_EVALUATED"
    assert statuses.get("ZOMBIE_LIQUIDITY") == "NOT_EVALUATED"


def test_fade_short_pattern_disabled_with_probe():
    """fade_long_only_v2：原 SHORT 溫和案例改為 FADE_SHORT_DISABLED。"""
    df = _fade_signal_df()
    ctx = {"price_position_pct": 50.0, "price_change_pct_24h": 1.0}
    a = detect_spike_fade_1m(df, ctx=ctx)
    begin_probe(school="SPIKE_FADE_1M", symbol="T", ctx=ctx)
    b = detect_spike_fade_1m(df, ctx=ctx)
    probe = take_probe()
    assert a is None and b is None
    assert probe["first_reject_code"] == "FADE_SHORT_DISABLED"
    statuses = {c["code"]: c["status"] for c in probe["conditions"]}
    assert statuses.get("FADE_SHORT_PATTERN") == "PASS"
    assert statuses.get("FADE_SHORT_DISABLED") == "FAIL"
    assert statuses.get("FADE_LONG_PATTERN") == "NOT_EVALUATED"


def test_insufficient_bars():
    df = _bars(5)
    begin_probe(school="SPIKE_MOMO_1M", symbol="T")
    assert detect_spike_momo_1m(df) is None
    probe = take_probe()
    assert probe["first_reject_code"] == "INSUFFICIENT_CLOSED_BARS"


def test_vol_ma_zero():
    df = _momo_signal_df()
    df = df.copy()
    # 分母窗口全 0
    df.loc[df.index[:-1], "volume"] = 0.0
    df.loc[df.index[-1], "volume"] = 1000.0
    begin_probe(school="SPIKE_MOMO_1M", symbol="T", ctx={"price_position_pct": 80})
    assert detect_spike_momo_1m(df, ctx={"price_position_pct": 80}) is None
    probe = take_probe()
    assert probe["first_reject_code"] == "VOL_MA_ZERO"


def test_unclosed_bar_excluded():
    """輸入含未收盤根時，detect 使用的訊號根不得是未收盤根。"""
    df = _momo_signal_df()
    # 追加一根「正在走」的 K（open_time = 目前分鐘）
    cur = int(time.time() * 1000) // 60_000 * 60_000
    forming = {
        "open_time": cur,
        "open": 102.0,
        "high": 110.0,
        "low": 101.0,
        "close": 109.0,
        "volume": 99999.0,
    }
    df2 = pd.concat([df, pd.DataFrame([forming])], ignore_index=True)
    now_ms = cur + 2_000  # 開盤後 2 秒，未收盤
    ctx = {"price_position_pct": 80.0}
    begin_probe(school="SPIKE_MOMO_1M", symbol="T", ctx=ctx)
    sig = detect_spike_momo_1m(df2, ctx=ctx, now_ms=now_ms)
    probe = take_probe()
    assert cur in (probe.get("excluded_unclosed_open_times") or [])
    assert probe.get("signal_bar_open_time") != cur
    # 與不含 forming 根、同一 now_ms 結果一致
    begin_probe(school="SPIKE_MOMO_1M", symbol="T", ctx=ctx)
    sig2 = detect_spike_momo_1m(df, ctx=ctx, now_ms=now_ms)
    take_probe()
    assert (sig is None) == (sig2 is None)
    if sig is not None:
        assert sig["side"] == sig2["side"]
        assert abs(sig["entry"] - sig2["entry"]) < 1e-12


def test_body_boundary():
    """實體剛好在門檻：不改決策語意（>= body_min）。"""
    df = _momo_signal_df()
    # 製造 body_ratio 剛好 0.65：range=1, body=0.65
    df = df.copy()
    i = df.index[-1]
    df.loc[i, "open"] = 100.0
    df.loc[i, "close"] = 100.65
    df.loc[i, "high"] = 101.0
    df.loc[i, "low"] = 100.0
    df.loc[i, "volume"] = 1000.0
    ctx = {"price_position_pct": 80.0}
    body_min = float(config.SPIKE_MOMO_BODY_MIN)
    # 0.65/1.0 = 0.65
    a = detect_spike_momo_1m(df, ctx=ctx)
    begin_probe(school="SPIKE_MOMO_1M", symbol="T", ctx=ctx)
    b = detect_spike_momo_1m(df, ctx=ctx)
    take_probe()
    assert a == b
    # 略降 body
    df2 = df.copy()
    df2.loc[i, "close"] = 100.64  # 0.64 < 0.65
    assert detect_spike_momo_1m(df2, ctx=ctx) is None
    assert body_min == 0.65


def test_diag_write_failure_does_not_change_signal(monkeypatch=None):
    df = _momo_signal_df()
    ctx = {"price_position_pct": 80.0}
    # 即使 warn 被呼叫，detect 結果不變
    begin_probe(school="SPIKE_MOMO_1M", symbol="T", ctx=ctx)
    sig = detect_spike_momo_1m(df, ctx=ctx)
    take_probe()
    warn_write_error("test_path", RuntimeError("boom"))
    sig2 = detect_spike_momo_1m(df, ctx=ctx)
    assert sig is not None and sig2 is not None
    assert sig["side"] == sig2["side"]
    assert abs(sig["entry"] - sig2["entry"]) < 1e-12


if __name__ == "__main__":
    test_momo_signal_unchanged_with_probe()
    test_momo_no_signal_vol_reject_stable()
    test_fade_short_pattern_disabled_with_probe()
    test_insufficient_bars()
    test_vol_ma_zero()
    test_unclosed_bar_excluded()
    test_body_boundary()
    test_diag_write_failure_does_not_change_signal()
    print("all ok")
