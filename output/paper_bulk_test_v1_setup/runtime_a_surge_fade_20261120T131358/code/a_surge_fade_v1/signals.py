"""Surge → weaken state machine (completed 1m bars only; no future peek)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .util import now_iso


@dataclass
class Bar:
    open_time: int  # ms
    open: float
    high: float
    low: float
    close: float
    close_time: int


@dataclass
class SymbolState:
    symbol: str
    phase: str = "IDLE"  # IDLE|WATCH|BROKEN|REBOUND|ARMED|SIGNALED|IN_TRADE
    surge_pct_24h: Optional[float] = None
    surge_pct_1h: Optional[float] = None
    surge_pct_4h: Optional[float] = None
    surge_bucket: Optional[str] = None  # 50_100 | ge_100
    H: Optional[float] = None
    break_bar_ot: Optional[int] = None
    R: Optional[float] = None
    L: Optional[float] = None
    rebound_bar_ot: Optional[int] = None
    last_update_iso: str = ""
    notes: list[str] = field(default_factory=list)
    signal_id: Optional[str] = None
    S: Optional[float] = None  # confirm close
    confirm_bar_ot: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SymbolState":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def _pct(a: float, b: float) -> Optional[float]:
    if b <= 0 or a <= 0:
        return None
    return (a / b) - 1.0


def compute_surge_metrics(bars: list[Bar]) -> dict[str, Any]:
    """bars: ascending completed 1m. Need >= 1441 for 24h close-to-close."""
    out: dict[str, Any] = {
        "ok": False,
        "reason": None,
        "pct_24h": None,
        "pct_1h": None,
        "pct_4h": None,
        "bucket": None,
        "n_bars": len(bars),
    }
    if len(bars) < 1441:
        out["reason"] = "INSUFFICIENT_24H_HISTORY"
        return out
    last = bars[-1]
    # 24h ago = 1440 minutes earlier bar
    b24 = bars[-1 - 1440]
    pct24 = _pct(last.close, b24.close)
    out["pct_24h"] = pct24
    if len(bars) >= 61:
        out["pct_1h"] = _pct(last.close, bars[-1 - 60].close)
    if len(bars) >= 241:
        out["pct_4h"] = _pct(last.close, bars[-1 - 240].close)
    if pct24 is None:
        out["reason"] = "BAD_PRICE"
        return out
    if pct24 < config.SURGE_MIN_PCT_24H:
        out["reason"] = "BELOW_SURGE_MIN"
        return out
    out["ok"] = True
    out["bucket"] = "ge_100" if pct24 >= config.SURGE_BUCKET_MID else "50_100"
    return out


def step_state(st: SymbolState, bars: list[Bar]) -> tuple[SymbolState, Optional[dict[str, Any]]]:
    """Advance state with newly completed bars. Returns (state, optional_signal).

    Signal is emitted only after confirm bar is complete; caller must not fill
    inside that bar.
    """
    signal: Optional[dict[str, Any]] = None
    if st.phase == "IN_TRADE" or st.phase == "SIGNALED":
        # wait for trade cycle end externally to reset
        return st, None
    if len(bars) < config.BREAK_LOOKBACK + 2:
        return st, None

    metrics = compute_surge_metrics(bars)
    st.last_update_iso = now_iso()
    st.surge_pct_24h = metrics.get("pct_24h")
    st.surge_pct_1h = metrics.get("pct_1h")
    st.surge_pct_4h = metrics.get("pct_4h")

    if not metrics["ok"]:
        if st.phase in ("WATCH", "BROKEN", "REBOUND", "ARMED") and metrics["reason"] in (
            "BELOW_SURGE_MIN",
            "INSUFFICIENT_24H_HISTORY",
            "BAD_PRICE",
        ):
            st.notes.append(f"clear_watch:{metrics['reason']}:{bars[-1].open_time}")
            st.phase = "IDLE"
            st.H = st.R = st.L = None
            st.break_bar_ot = st.rebound_bar_ot = None
        return st, None

    st.surge_bucket = metrics["bucket"]
    last = bars[-1]

    if st.phase == "IDLE":
        st.phase = "WATCH"
        st.notes.append(f"enter_watch:{last.open_time}:pct24={metrics['pct_24h']}")

    if st.phase == "WATCH":
        # break: close < min(low of prior 5 completed bars excluding current)
        prior = bars[-(config.BREAK_LOOKBACK + 1) : -1]
        if len(prior) < config.BREAK_LOOKBACK:
            return st, None
        low5 = min(b.low for b in prior)
        high5 = max(b.high for b in prior)
        if last.close < low5:
            st.phase = "BROKEN"
            st.H = high5
            st.break_bar_ot = last.open_time
            st.notes.append(f"break:{last.open_time}:close={last.close}:low5={low5}:H={high5}")
        return st, None

    if st.phase == "BROKEN":
        # wait first completed bar with close > previous close; max high since break < H
        # find bars after break
        after = [b for b in bars if st.break_bar_ot is not None and b.open_time > st.break_bar_ot]
        if not after:
            return st, None
        # invalidate if any high since break >= H before rebound found
        if st.H is not None and max(b.high for b in after) >= st.H:
            st.notes.append(f"fail_H_before_rebound:{last.open_time}")
            st.phase = "WATCH"
            st.H = st.R = st.L = None
            st.break_bar_ot = None
            return st, None
        # need previous close comparison among after bars
        if len(after) < 2:
            # compare to break bar close
            prev_close = next(b.close for b in bars if b.open_time == st.break_bar_ot)
            cand = after[-1]
            if cand.close > prev_close:
                st.phase = "REBOUND"
                st.R = cand.high
                st.L = cand.low
                st.rebound_bar_ot = cand.open_time
                st.notes.append(f"rebound:{cand.open_time}:R={st.R}:L={st.L}")
            return st, None
        cand = after[-1]
        prev = after[-2]
        if cand.close > prev.close:
            # also ensure highs from break to cand < H
            window = [b for b in after if b.open_time <= cand.open_time]
            if st.H is not None and max(b.high for b in window) >= st.H:
                st.notes.append(f"fail_H_on_rebound:{cand.open_time}")
                st.phase = "WATCH"
                st.H = st.R = st.L = None
                st.break_bar_ot = None
                return st, None
            st.phase = "REBOUND"
            st.R = cand.high
            st.L = cand.low
            st.rebound_bar_ot = cand.open_time
            st.notes.append(f"rebound:{cand.open_time}:R={st.R}:L={st.L}")
        return st, None

    if st.phase == "REBOUND":
        # from next bar after rebound: wait close < L; invalidate if high > R
        after = [b for b in bars if st.rebound_bar_ot is not None and b.open_time > st.rebound_bar_ot]
        if not after:
            return st, None
        for b in after:
            if st.R is not None and b.high > st.R:
                st.notes.append(f"fail_break_R:{b.open_time}:high={b.high}:R={st.R}")
                st.phase = "WATCH"
                st.H = st.R = st.L = None
                st.break_bar_ot = st.rebound_bar_ot = None
                return st, None
            if st.L is not None and b.close < st.L:
                # confirm bar complete → signal (fill only after this bar)
                st.phase = "SIGNALED"
                st.S = b.close
                st.confirm_bar_ot = b.open_time
                st.signal_id = f"{st.symbol}:{b.open_time}:SHORT:{config.STRATEGY_VERSION}"
                signal = {
                    "signal_id": st.signal_id,
                    "symbol": st.symbol,
                    "side": "SHORT",
                    "S": st.S,
                    "confirm_bar_ot": b.open_time,
                    "confirm_close_time": b.close_time,
                    "H": st.H,
                    "R": st.R,
                    "L": st.L,
                    "surge_pct_24h": st.surge_pct_24h,
                    "surge_pct_1h": st.surge_pct_1h,
                    "surge_pct_4h": st.surge_pct_4h,
                    "surge_bucket": st.surge_bucket,
                    "strategy_version": config.STRATEGY_VERSION,
                    "emitted_at": now_iso(),
                    "fill_allowed_after_ms": b.close_time + 1,
                }
                st.notes.append(f"signal:{st.signal_id}:S={st.S}")
                return st, signal
        return st, None

    return st, None


def reset_after_trade_cycle(st: SymbolState) -> SymbolState:
    st.phase = "IDLE"
    st.H = st.R = st.L = None
    st.break_bar_ot = st.rebound_bar_ot = st.confirm_bar_ot = None
    st.signal_id = None
    st.S = None
    st.notes.append(f"reset_after_cycle:{now_iso()}")
    return st
