"""Breakout follow signal detection (closed 1m only)."""

from __future__ import annotations

import time
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import config
from .market_data import closed_klines, wilder_atr
from .util import ms_to_iso


def detect_breakout(
    df_1m: pd.DataFrame,
    *,
    now_ms: Optional[int] = None,
    book: Optional[dict[str, Any]] = None,
    data_fetched_at: Optional[float] = None,
) -> dict[str, Any]:
    """
    回傳診斷結構；signal 有值才算通過型態。
    S = 最後一根已收盤；區間/量/ATR 基準皆排除 S。
    """
    now_ms = now_ms or int(time.time() * 1000)
    probe: dict[str, Any] = {
        "result": "NO_SIGNAL",
        "first_reject_code": None,
        "conditions": [],
        "signal": None,
        "bars_raw": int(len(df_1m)) if df_1m is not None else 0,
        "bars_closed": 0,
        "excluded_unclosed": [],
        "signal_open_time": None,
        "data_fetched_at": data_fetched_at,
        "eval_now_ms": now_ms,
    }

    def cond(code: str, status: str, **kw: Any) -> None:
        probe["conditions"].append({"code": code, "status": status, **kw})

    def reject(code: str, **kw: Any) -> dict[str, Any]:
        if probe["first_reject_code"] is None:
            probe["first_reject_code"] = code
            probe["result"] = "REJECT"
        cond(code, "FAIL", **kw)
        return probe

    def mark_rest_ne(codes: list[str]) -> None:
        seen = {c["code"] for c in probe["conditions"]}
        for c in codes:
            if c not in seen:
                cond(c, "NOT_EVALUATED")

    rest_after = {
        "INSUFFICIENT_BARS": ["VOL_MA", "VOL_X", "RANGE", "ATR", "BREAKOUT", "SPREAD", "CHASE", "FRESHNESS"],
        "DATA_INVALID": ["VOL_MA", "VOL_X", "RANGE", "ATR", "BREAKOUT", "SPREAD", "CHASE", "FRESHNESS"],
        "VOL_MA_ZERO": ["VOL_X", "RANGE", "ATR", "BREAKOUT", "SPREAD", "CHASE", "FRESHNESS"],
        "VOL_X": ["RANGE", "ATR", "BREAKOUT", "SPREAD", "CHASE", "FRESHNESS"],
        "RANGE": ["ATR", "BREAKOUT", "SPREAD", "CHASE", "FRESHNESS"],
        "ATR": ["BREAKOUT", "SPREAD", "CHASE", "FRESHNESS"],
        "BREAKOUT": ["SPREAD", "CHASE", "FRESHNESS"],
        "SPREAD": ["CHASE", "FRESHNESS"],
        "CHASE": ["FRESHNESS"],
    }

    if df_1m is None or df_1m.empty:
        mark_rest_ne(rest_after["INSUFFICIENT_BARS"])
        return reject("INSUFFICIENT_BARS", actual=0, threshold=config.RANGE_LOOKBACK + config.ATR_PERIOD + 2)

    raw_last = int(df_1m["open_time"].iloc[-1]) if "open_time" in df_1m.columns else None
    closed = closed_klines(df_1m, now_ms=now_ms)
    probe["bars_closed"] = len(closed)
    if raw_last is not None and (closed.empty or int(closed["open_time"].iloc[-1]) != raw_last):
        probe["excluded_unclosed"] = [raw_last]

    need = max(config.RANGE_LOOKBACK, config.VOLUME_MA_PERIOD) + config.ATR_PERIOD + 2
    if len(closed) < need:
        mark_rest_ne(rest_after["INSUFFICIENT_BARS"])
        return reject("INSUFFICIENT_BARS", actual=len(closed), threshold=need, unit="bars")
    cond("INSUFFICIENT_BARS", "PASS", actual=len(closed), threshold=need)

    # freshness of kline data
    if data_fetched_at is not None:
        age = time.time() - float(data_fetched_at)
        if age > config.KLINE_MAX_AGE_SEC:
            mark_rest_ne(rest_after["DATA_INVALID"])
            return reject("STALE_KLINE", actual=age, threshold=config.KLINE_MAX_AGE_SEC, unit="sec")

    s_idx = len(closed) - 1
    s = closed.iloc[s_idx]
    s_ot = int(s["open_time"])
    probe["signal_open_time"] = s_ot
    probe["signal_open_time_iso"] = ms_to_iso(s_ot)

    # delay: signal bar close should not be too old relative to now
    s_close_ms = int(s["close_time"]) if "close_time" in closed.columns else s_ot + 60_000
    delay_sec = (now_ms - s_close_ms) / 1000.0
    if delay_sec > config.SIGNAL_MAX_DELAY_SEC:
        mark_rest_ne(rest_after["DATA_INVALID"])
        return reject("SIGNAL_DELAY", actual=delay_sec, threshold=config.SIGNAL_MAX_DELAY_SEC, unit="sec")

    prior = closed.iloc[:s_idx]
    if len(prior) < max(config.RANGE_LOOKBACK, config.VOLUME_MA_PERIOD, config.ATR_PERIOD + 1):
        mark_rest_ne(rest_after["INSUFFICIENT_BARS"])
        return reject("INSUFFICIENT_PRIOR", actual=len(prior))

    vol_win = prior.tail(config.VOLUME_MA_PERIOD)["volume"].to_numpy(dtype=float)
    vol_ma = float(np.mean(vol_win)) if len(vol_win) else 0.0
    probe["vol_metric"] = {
        "numerator_open_time": s_ot,
        "numerator_volume": float(s["volume"]),
        "denominator_bars": config.VOLUME_MA_PERIOD,
        "denominator_open_time_first": int(prior.tail(config.VOLUME_MA_PERIOD)["open_time"].iloc[0]),
        "denominator_open_time_last": int(prior.tail(config.VOLUME_MA_PERIOD)["open_time"].iloc[-1]),
        "denominator_mean": vol_ma,
        "exclude_signal_bar": True,
    }
    if vol_ma <= 0 or not np.isfinite(vol_ma):
        mark_rest_ne(rest_after["VOL_MA_ZERO"])
        return reject("VOL_MA_ZERO", actual=vol_ma)
    cond("VOL_MA", "PASS", actual=vol_ma)

    vol_x = float(s["volume"]) / vol_ma
    probe["vol_metric"]["vol_x"] = vol_x
    if not np.isfinite(vol_x) or vol_x < 0:
        mark_rest_ne(rest_after["VOL_X"])
        return reject("DATA_INVALID", actual=vol_x, detail="non_finite_vol_x")
    if vol_x < config.VOL_X_MIN:
        mark_rest_ne(rest_after["VOL_X"])
        return reject("VOL_X", actual=vol_x, op="<", threshold=config.VOL_X_MIN)
    cond("VOL_X", "PASS", actual=vol_x, threshold=config.VOL_X_MIN)

    range_df = prior.tail(config.RANGE_LOOKBACK)
    range_high = float(range_df["high"].max())
    range_low = float(range_df["low"].min())
    probe["range"] = {
        "high": range_high,
        "low": range_low,
        "lookback": config.RANGE_LOOKBACK,
        "first_ot": int(range_df["open_time"].iloc[0]),
        "last_ot": int(range_df["open_time"].iloc[-1]),
        "excludes_S": True,
    }
    if range_high <= range_low or not np.isfinite(range_high):
        mark_rest_ne(rest_after["RANGE"])
        return reject("RANGE_INVALID", actual=[range_low, range_high])
    cond("RANGE", "PASS", actual={"high": range_high, "low": range_low})

    atr_base = prior  # exclude S
    atr = wilder_atr(atr_base.tail(config.ATR_PERIOD + 5), period=config.ATR_PERIOD)
    if atr is None or atr <= 0 or not np.isfinite(atr):
        mark_rest_ne(rest_after["ATR"])
        return reject("ATR_INVALID", actual=atr)
    atr_time = ms_to_iso(int(atr_base["open_time"].iloc[-1]))
    probe["atr"] = {"value": atr, "period": config.ATR_PERIOD, "asof_open_time": int(atr_base["open_time"].iloc[-1]), "asof_iso": atr_time}
    cond("ATR", "PASS", actual=atr)

    s_close = float(s["close"])
    long_level = range_high + config.BREAKOUT_BUFFER_ATR * atr
    short_level = range_low - config.BREAKOUT_BUFFER_ATR * atr
    side = None
    if s_close > long_level:
        side = "LONG"
    elif s_close < short_level:
        side = "SHORT"
    else:
        mark_rest_ne(rest_after["BREAKOUT"])
        return reject(
            "NO_BREAKOUT",
            actual=s_close,
            threshold={"long_gt": long_level, "short_lt": short_level},
        )
    cond("BREAKOUT", "PASS", actual={"side": side, "close": s_close, "long_level": long_level, "short_level": short_level})

    # spread / quote freshness
    if book is None:
        mark_rest_ne(rest_after["SPREAD"])
        return reject("QUOTE_MISSING")
    q_age = time.time() - float(book.get("fetched_at") or 0)
    if q_age > config.BOOK_TICKER_MAX_AGE_SEC:
        mark_rest_ne(rest_after["SPREAD"])
        return reject("STALE_QUOTE", actual=q_age, threshold=config.BOOK_TICKER_MAX_AGE_SEC)
    spread = float(book.get("spread") or 999)
    if spread > config.MAX_SPREAD:
        mark_rest_ne(rest_after["SPREAD"])
        return reject("SPREAD", actual=spread, threshold=config.MAX_SPREAD)
    cond("SPREAD", "PASS", actual=spread, threshold=config.MAX_SPREAD)

    # estimated fill with adverse slippage must remain in breakout direction & chase limit
    bid = float(book["bid"])
    ask = float(book["ask"])
    if side == "LONG":
        raw = ask * (1 + config.SLIPPAGE_ADVERSE)
        boundary = range_high
        chase = (raw - boundary) / atr if atr > 0 else 999
        if raw <= long_level:
            mark_rest_ne(rest_after["CHASE"])
            return reject("FILL_NOT_BEYOND_BREAKOUT", actual=raw, threshold=long_level)
    else:
        raw = bid * (1 - config.SLIPPAGE_ADVERSE)
        boundary = range_low
        chase = (boundary - raw) / atr if atr > 0 else 999
        if raw >= short_level:
            mark_rest_ne(rest_after["CHASE"])
            return reject("FILL_NOT_BEYOND_BREAKOUT", actual=raw, threshold=short_level)

    if chase > config.MAX_CHASE_ATR:
        mark_rest_ne(rest_after["CHASE"])
        return reject("CHASE", actual=chase, threshold=config.MAX_CHASE_ATR, unit="ATR")
    cond("CHASE", "PASS", actual=chase, threshold=config.MAX_CHASE_ATR)
    cond("FRESHNESS", "PASS", actual={"kline_delay_sec": delay_sec, "quote_age_sec": q_age})

    probe["result"] = "SIGNAL"
    probe["first_reject_code"] = None
    orig_side = side
    direction_flip = bool(getattr(config, "SIGNAL_DIRECTION_FLIP", False))
    if direction_flip:
        # 閘門／追價已依原突破方向驗證；翻轉後依新方向重建成交價（不重跑 chase）
        side = "SHORT" if side == "LONG" else "LONG"
        if side == "LONG":
            raw = ask * (1 + config.SLIPPAGE_ADVERSE)
        else:
            raw = bid * (1 - config.SLIPPAGE_ADVERSE)
    # 訊號參考價：當下 mid（限價進場以此為準；市價路徑仍用 raw）
    signal_ref = (bid + ask) / 2.0
    limit_enabled = bool(getattr(config, "LIMIT_ENTRY_ENABLED", False))
    offset = float(getattr(config, "LIMIT_ENTRY_OFFSET_PCT", 0.005) or 0.005)
    if limit_enabled and offset > 0 and signal_ref > 0:
        if side == "LONG":
            limit_px = signal_ref * (1.0 - offset)
        else:
            limit_px = signal_ref * (1.0 + offset)
        # 限價模式：est_fill 改為限價（不再用追價市價 raw）
        raw = limit_px
    else:
        limit_px = None
    probe["signal"] = {
        "side": side,
        "orig_side": orig_side,
        "direction_flip": direction_flip,
        "signal_open_time": s_ot,
        "signal_close": s_close,
        "est_fill_price": raw,
        "signal_ref_price": signal_ref,
        "limit_entry_enabled": limit_enabled,
        "limit_price": limit_px,
        "limit_offset_pct": offset if limit_enabled else None,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "range_high": range_high,
        "range_low": range_low,
        "atr": atr,
        "atr_time": atr_time,
        "vol_x": vol_x,
        "long_level": long_level,
        "short_level": short_level,
        "chase_atr": chase,
        "simulation_assumption": config.SIMULATION_ASSUMPTIONS,
    }
    return probe
