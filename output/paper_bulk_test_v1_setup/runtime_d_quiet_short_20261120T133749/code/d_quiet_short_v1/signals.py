"""15m quiet-range signal FSM for D (completed bars only)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .util import median, now_iso


@dataclass
class Bar15:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int

    def bar_amp(self) -> float:
        if self.open <= 0:
            return 999.0
        return (self.high - self.low) / self.open


@dataclass
class SymbolState:
    symbol: str
    phase: str = "IDLE"  # IDLE | ARMED_SIGNAL | IN_TRADE | COOLDOWN_REBUILD
    last_signal_id: Optional[str] = None
    last_signal: Optional[dict[str, Any]] = None
    rebuild_after_close_ms: Optional[int] = None  # fresh bars must open after this
    rebuild_need_bars: int = 1  # 4 after vol-exit; 1 after other exits
    cooldown_reason: Optional[str] = None
    updated_at: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SymbolState":
        return cls(
            symbol=d.get("symbol", ""),
            phase=d.get("phase", "IDLE"),
            last_signal_id=d.get("last_signal_id"),
            last_signal=d.get("last_signal"),
            rebuild_after_close_ms=d.get("rebuild_after_close_ms"),
            rebuild_need_bars=int(d.get("rebuild_need_bars") or 1),
            cooldown_reason=d.get("cooldown_reason"),
            updated_at=d.get("updated_at", ""),
            history=list(d.get("history") or []),
        )


def _continuous(bars: list[Bar15]) -> bool:
    if len(bars) < 2:
        return True
    for a, b in zip(bars, bars[1:]):
        if b.open_time - a.open_time != config.KLINE_MS:
            return False
        if b.open_time != a.close_time + 1 and b.open_time != a.open_time + config.KLINE_MS:
            # Binance: next open == prev open + 15m; close_time = open+899999
            if abs(b.open_time - (a.open_time + config.KLINE_MS)) > 1:
                return False
    return True


def evaluate_quiet(bars: list[Bar15]) -> dict[str, Any]:
    """Evaluate last 4 completed 15m bars. Never uses incomplete bar."""
    out: dict[str, Any] = {
        "ok": False,
        "reason": "",
        "range_amp": None,
        "bar_amps": [],
        "B": None,
        "T": None,
        "slope": None,
        "bars": [],
        "close": None,
    }
    if len(bars) < config.QUIET_BARS:
        out["reason"] = "INSUFFICIENT_BARS"
        return out
    window = bars[-config.QUIET_BARS :]
    if not _continuous(window):
        out["reason"] = "NON_CONTINUOUS"
        return out
    for b in window:
        if min(b.open, b.high, b.low, b.close) <= 0 or b.high < b.low:
            out["reason"] = "INVALID_OHLC"
            return out
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    close = window[-1].close
    if close <= 0:
        out["reason"] = "BAD_CLOSE"
        return out
    range_amp = (hi - lo) / close
    bar_amps = [b.bar_amp() for b in window]
    out["range_amp"] = range_amp
    out["bar_amps"] = bar_amps
    out["close"] = close
    out["bars"] = [
        {
            "open_time": b.open_time,
            "close_time": b.close_time,
            "o": b.open,
            "h": b.high,
            "l": b.low,
            "c": b.close,
            "amp": a,
        }
        for b, a in zip(window, bar_amps)
    ]
    # slope for post-hoc only (close of first vs last of window)
    out["slope"] = (window[-1].close - window[0].close) / window[0].close if window[0].close else None
    B = median(bar_amps)
    T = max(config.VOL_EXIT_FLOOR, 2.0 * B)
    out["B"] = B
    out["T"] = T
    if range_amp > config.RANGE_AMP_MAX:
        out["reason"] = "RANGE_AMP_FAIL"
        return out
    if any(a > config.BAR_AMP_MAX for a in bar_amps):
        out["reason"] = "BAR_AMP_FAIL"
        return out
    out["ok"] = True
    out["reason"] = "QUIET_OK"
    return out


def rebuild_ready(st: SymbolState, bars: list[Bar15]) -> bool:
    """Fresh completed bars must open AFTER exit's 15m bar open; incomplete exit bar excluded."""
    if st.rebuild_after_close_ms is None:
        return True
    cutoff = int(st.rebuild_after_close_ms)
    need = max(1, int(st.rebuild_need_bars or 1))
    fresh = [b for b in bars if b.open_time > cutoff]
    return len(fresh) >= need


def make_signal_id(symbol: str, bar_close_time: int) -> str:
    return f"{symbol}:{bar_close_time}:SHORT:{config.STRATEGY_VERSION}"


def try_emit_signal(
    st: SymbolState,
    bars: list[Bar15],
    *,
    P: float,
    price_source: str,
    price_ts: float,
    spread: float,
) -> tuple[SymbolState, Optional[dict[str, Any]]]:
    """Emit at most one signal per completed-bar close; never reuse same signal_id."""
    st.updated_at = now_iso()
    if st.phase in ("ARMED_SIGNAL", "IN_TRADE"):
        return st, None
    if st.phase == "COOLDOWN_REBUILD" and not rebuild_ready(st, bars):
        return st, None

    ev = evaluate_quiet(bars)
    if not ev["ok"]:
        if st.phase == "COOLDOWN_REBUILD" and rebuild_ready(st, bars):
            # eligibility failed after rebuild window — stay idle-ish
            st.phase = "IDLE"
            st.cooldown_reason = ev["reason"]
        return st, None

    if not rebuild_ready(st, bars):
        return st, None

    last = bars[-1]
    sid = make_signal_id(st.symbol, last.close_time)
    if sid == st.last_signal_id:
        return st, None

    signal = {
        "signal_id": sid,
        "symbol": st.symbol,
        "side": "SHORT",
        "strategy_version": config.STRATEGY_VERSION,
        "bar_open_time": last.open_time,
        "bar_close_time": last.close_time,
        "P": float(P),
        "price_source": price_source,
        "price_ts": float(price_ts),
        "spread": float(spread),
        "range_amp": ev["range_amp"],
        "bar_amps": ev["bar_amps"],
        "B": ev["B"],
        "T": ev["T"],
        "slope": ev["slope"],
        "bars": ev["bars"],
        "emitted_at": now_iso(),
        # fill only after confirm (signal uses completed bar already)
        "fill_allowed_after_ms": int(last.close_time) + 1,
    }
    st.phase = "ARMED_SIGNAL"
    st.last_signal_id = sid
    st.last_signal = signal
    st.cooldown_reason = None
    st.rebuild_after_close_ms = None
    st.history.append({"at": now_iso(), "event": "SIGNAL", "signal_id": sid})
    return st, signal


def mark_in_trade(st: SymbolState) -> SymbolState:
    st.phase = "IN_TRADE"
    st.updated_at = now_iso()
    return st


def after_exit(st: SymbolState, *, reason: str, exit_ts_ms: int, require_rebuild: bool) -> SymbolState:
    st.last_signal = None
    st.updated_at = now_iso()
    st.history.append({"at": now_iso(), "event": "EXIT_CLEAR", "reason": reason})
    # Floor to start of the 15m bar containing exit; that incomplete bar is excluded.
    bar_open = (int(exit_ts_ms) // config.KLINE_MS) * config.KLINE_MS
    st.rebuild_after_close_ms = bar_open
    st.rebuild_need_bars = config.QUIET_BARS if require_rebuild else 1
    st.phase = "COOLDOWN_REBUILD"
    st.cooldown_reason = reason
    return st


def quiet_still_valid(bars: list[Bar15]) -> tuple[bool, str]:
    ev = evaluate_quiet(bars)
    return bool(ev["ok"]), str(ev["reason"])
