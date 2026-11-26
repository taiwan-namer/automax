"""Sparse 1-second last-price series for H price_action_v2.

p(t) = last trade price in complete second t.
Missing seconds are NOT filled with prior prices.
Incomplete current second is excluded from evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SparseSecSeries:
    """Event-time sparse second prices (trade seconds only)."""

    max_keep_sec: int = 360
    _px: dict[int, float] = field(default_factory=dict)
    _secs: list[int] = field(default_factory=list)  # ascending unique
    last_trade_ts: float = 0.0
    last_price: Optional[float] = None

    def clear(self) -> None:
        self._px.clear()
        self._secs.clear()
        self.last_trade_ts = 0.0
        self.last_price = None

    def on_trade(self, event_ts: float, price: float) -> None:
        ts = float(event_ts)
        if self.last_trade_ts > 0 and ts + 1e-12 < self.last_trade_ts:
            return  # out of order
        sec = int(ts)
        px = float(price)
        self.last_trade_ts = ts
        self.last_price = px
        if sec not in self._px:
            self._secs.append(sec)
            self._secs.sort()
        self._px[sec] = px
        self._trim()

    def _trim(self) -> None:
        if not self._secs:
            return
        lo = self._secs[-1] - int(self.max_keep_sec)
        while self._secs and self._secs[0] < lo:
            old = self._secs.pop(0)
            self._px.pop(old, None)

    def latest_complete_sec(self, decision_ts: float) -> int:
        """Latest second that is fully closed at decision_ts."""
        return int(decision_ts) - 1

    def has(self, sec: int) -> bool:
        return int(sec) in self._px

    def p(self, sec: int) -> Optional[float]:
        return self._px.get(int(sec))

    def coverage(self, t: int, window_sec: int) -> Optional[float]:
        """Fraction of seconds with trades in [t-window, t] inclusive."""
        if window_sec < 0:
            return None
        t = int(t)
        w = int(window_sec)
        need = w + 1
        if need <= 0:
            return None
        hit = sum(1 for s in range(t - w, t + 1) if s in self._px)
        return hit / float(need)

    def ret(self, t: int, window_sec: int) -> Optional[float]:
        """ret_W(t)=p(t)/p(t-W)-1; both endpoints required, no substitute."""
        t = int(t)
        w = int(window_sec)
        a = self._px.get(t - w)
        b = self._px.get(t)
        if a is None or b is None or a <= 0:
            return None
        return (b / a) - 1.0

    def er(self, t: int, window_sec: int) -> float:
        """ER_W over available prices in [t-W, t]; gaps not interpolated."""
        t = int(t)
        w = int(window_sec)
        pts = [self._px[s] for s in range(t - w, t + 1) if s in self._px]
        if len(pts) < 2:
            return 0.0
        net = abs(pts[-1] - pts[0])
        path = 0.0
        for i in range(1, len(pts)):
            path += abs(pts[i] - pts[i - 1])
        if path <= 0:
            return 0.0
        return net / path

    def hl(
        self, t0: int, t1: int
    ) -> tuple[Optional[float], Optional[float], Optional[int], Optional[int]]:
        """Max/min and last-occurrence times in [t0, t1] inclusive."""
        t0, t1 = int(t0), int(t1)
        if t1 < t0:
            return None, None, None, None
        hi: Optional[float] = None
        lo: Optional[float] = None
        tau_h: Optional[int] = None
        tau_l: Optional[int] = None
        for s in range(t0, t1 + 1):
            px = self._px.get(s)
            if px is None:
                continue
            if hi is None or px >= hi:  # same price → latest time
                hi = px
                tau_h = s
            if lo is None or px <= lo:
                lo = px
                tau_l = s
        return hi, lo, tau_h, tau_l

    def prices_in(self, t0: int, t1: int) -> list[tuple[int, float]]:
        t0, t1 = int(t0), int(t1)
        out: list[tuple[int, float]] = []
        for s in range(t0, t1 + 1):
            px = self._px.get(s)
            if px is not None:
                out.append((s, px))
        return out

    def max_in(self, t0: int, t1: int) -> Optional[float]:
        hi, _, _, _ = self.hl(t0, t1)
        return hi

    def min_in(self, t0: int, t1: int) -> Optional[float]:
        _, lo, _, _ = self.hl(t0, t1)
        return lo
