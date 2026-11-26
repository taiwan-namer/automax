"""Event-time 3m bars and 1-second price grids from trade prints.

Only uses trades with event_ts <= decision_ts. Never backfills a closed bar's
final volume/H/L/C into earlier decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradePrint:
    event_ts: float  # exchange event time seconds
    price: float
    qty: float
    quote: float  # price * qty (quote notional)


@dataclass
class Bar3m:
    start_ts: float
    end_ts: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    quote_volume: float = 0.0
    trade_count: int = 0
    complete: bool = False

    def as_known_at(self, decision_ts: float) -> Optional["Bar3m"]:
        """Snapshot of bar state using only prints already applied (<= decision)."""
        if decision_ts < self.start_ts:
            return None
        return Bar3m(
            start_ts=self.start_ts,
            end_ts=self.end_ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            quote_volume=self.quote_volume,
            trade_count=self.trade_count,
            complete=self.complete and decision_ts >= self.end_ts,
        )


@dataclass
class PriceGrid1s:
    """Contiguous 1s last-price grid; marks gaps when stream hole detected."""

    prices: list[float] = field(default_factory=list)
    seconds: list[int] = field(default_factory=list)
    last_trade_ts: float = 0.0
    gap_unfilled: bool = False
    gap_reason: str = ""

    def ready(self, need: int) -> bool:
        return len(self.prices) >= need and not self.gap_unfilled


class BarBuilder3m:
    def __init__(self, bar_sec: int = 180):
        self.bar_sec = int(bar_sec)
        self.completed: list[Bar3m] = []
        self.current: Optional[Bar3m] = None

    def _floor_start(self, ts: float) -> float:
        return float(int(ts // self.bar_sec) * self.bar_sec)

    def on_trade(self, tr: TradePrint) -> None:
        start = self._floor_start(tr.event_ts)
        end = start + self.bar_sec
        if self.current is None:
            self.current = Bar3m(start_ts=start, end_ts=end, open=tr.price, high=tr.price, low=tr.price, close=tr.price)
        elif start > self.current.start_ts:
            # close prior only if we moved to a later bucket (prior becomes complete)
            self.current.complete = True
            self.completed.append(self.current)
            # if buckets were skipped, we do NOT invent empty completed bars
            self.current = Bar3m(start_ts=start, end_ts=end, open=tr.price, high=tr.price, low=tr.price, close=tr.price)
        elif start < self.current.start_ts:
            # out-of-order older trade — ignore for live path (no rewrite of past decisions)
            return
        b = self.current
        if b.trade_count == 0:
            b.open = tr.price
            b.high = tr.price
            b.low = tr.price
        else:
            b.high = max(b.high, tr.price)
            b.low = min(b.low, tr.price)
        b.close = tr.price
        b.quote_volume += float(tr.quote)
        b.trade_count += 1

    def baseline_median(self, lookback: int) -> Optional[float]:
        done = [b for b in self.completed if b.complete]
        if len(done) < lookback:
            return None
        vals = sorted(float(b.quote_volume) for b in done[-lookback:])
        n = len(vals)
        mid = n // 2
        if n % 2:
            return vals[mid]
        return 0.5 * (vals[mid - 1] + vals[mid])

    def known_current(self, decision_ts: float) -> Optional[Bar3m]:
        if self.current is None:
            return None
        return self.current.as_known_at(decision_ts)


class SecondPriceTracker:
    """Build last-60 complete 1s price points with carry-forward; detect gaps."""

    def __init__(self, window: int = 60, max_gap_carry_sec: float = 3.0):
        self.window = int(window)
        self.max_gap_carry_sec = float(max_gap_carry_sec)
        self._last_price: Optional[float] = None
        self._last_trade_ts: float = 0.0
        self._last_sec: Optional[int] = None
        self.grid = PriceGrid1s()

    @property
    def gap_unfilled(self) -> bool:
        return bool(self.grid.gap_unfilled)

    @property
    def gap_reason(self) -> str:
        return str(self.grid.gap_reason or "")

    def on_trade(self, tr: TradePrint) -> None:
        sec = int(tr.event_ts)
        if self._last_trade_ts > 0 and tr.event_ts + 1e-9 < self._last_trade_ts:
            # out of order — ignore
            return
        if self._last_trade_ts > 0 and (tr.event_ts - self._last_trade_ts) > self.max_gap_carry_sec:
            self.grid.gap_unfilled = True
            self.grid.gap_reason = "TRADE_STREAM_GAP"
            # reset grid; must re-accumulate
            self.grid.prices.clear()
            self.grid.seconds.clear()
            self._last_sec = None
        self._last_price = float(tr.price)
        self._last_trade_ts = float(tr.event_ts)
        self.grid.last_trade_ts = self._last_trade_ts
        if self.grid.gap_unfilled and self._last_sec is None:
            # after gap, start fresh from this second
            self.grid.gap_unfilled = False
            self.grid.gap_reason = ""
        if self._last_sec is None:
            self._last_sec = sec
            self.grid.prices.append(self._last_price)
            self.grid.seconds.append(sec)
        elif sec == self._last_sec:
            self.grid.prices[-1] = self._last_price
        elif sec > self._last_sec:
            # fill intermediate seconds by carry-forward ONLY if gap within limit
            for s in range(self._last_sec + 1, sec):
                # carry without a trade in that second — allowed only if stream not gapped
                self.grid.prices.append(self._last_price)
                self.grid.seconds.append(s)
            self.grid.prices.append(self._last_price)
            self.grid.seconds.append(sec)
            self._last_sec = sec
            # trim
            if len(self.grid.prices) > self.window:
                over = len(self.grid.prices) - self.window
                self.grid.prices = self.grid.prices[over:]
                self.grid.seconds = self.grid.seconds[over:]
        # else older second ignored

    def stale(self, now_ts: float, stale_sec: float) -> bool:
        if self._last_trade_ts <= 0:
            return True
        return (now_ts - self._last_trade_ts) > stale_sec

    def last_n_prices(self, n: int) -> list[float]:
        if len(self.grid.prices) < n:
            return []
        return list(self.grid.prices[-n:])
