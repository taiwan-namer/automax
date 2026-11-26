"""Surge-event state machine + continuation window (event-time only).

States: OBSERVING | RESERVED | CONSUMED | EXPIRED | SUPERSEDED | INVALID
Window: source [T,T+3m), cont1 [T+3m,T+6m), cont2 [T+6m,T+9m); expires_at=T+9m.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config


STATE_OBSERVING = "OBSERVING"
STATE_RESERVED = "RESERVED"
STATE_CONSUMED = "CONSUMED"
STATE_EXPIRED = "EXPIRED"
STATE_SUPERSEDED = "SUPERSEDED"
STATE_INVALID = "INVALID"

ACTIVE_STATES = frozenset({STATE_OBSERVING, STATE_RESERVED})
TERMINAL_STATES = frozenset(
    {STATE_CONSUMED, STATE_EXPIRED, STATE_SUPERSEDED, STATE_INVALID}
)


def bar_sec() -> float:
    return float(config.BAR_SEC)


def expires_at_for(source_bar_open_time: float) -> float:
    return float(source_bar_open_time) + 3.0 * bar_sec()


def continuation_index(source_bar_open_time: float, bar_start_ts: float) -> Optional[int]:
    """Return 1 or 2 if bar_start is a continuation bar; 0 if still on source; None if outside."""
    t = float(source_bar_open_time)
    b = float(bar_start_ts)
    bs = bar_sec()
    if abs(b - t) < 1e-9:
        return 0
    if abs(b - (t + bs)) < 1e-9:
        return 1
    if abs(b - (t + 2.0 * bs)) < 1e-9:
        return 2
    # half-open windows by event-time membership (tolerate float)
    if t <= b < t + bs:
        return 0
    if t + bs <= b < t + 2.0 * bs:
        return 1
    if t + 2.0 * bs <= b < t + 3.0 * bs:
        return 2
    return None


@dataclass
class SurgeEvent:
    event_id: str
    symbol: str
    source_bar_open_time: float
    detected_at: float
    baseline_frozen: float
    source_rvol_at_detection: float
    expires_at: float
    state: str = STATE_OBSERVING
    consumed_at: Optional[float] = None
    invalidation_reason: Optional[str] = None
    reserved_order_id: Optional[str] = None
    reserved_at: Optional[float] = None
    # bars that already emitted an origin surge detection (no re-arm / no expire reset)
    origin_detected_bars: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SurgeEvent":
        from dataclasses import fields as dc_fields

        known = {f.name for f in dc_fields(cls)}
        raw = {k: v for k, v in d.items() if k in known}
        ev = cls(
            event_id=str(raw.get("event_id") or ""),
            symbol=str(raw.get("symbol") or ""),
            source_bar_open_time=float(raw.get("source_bar_open_time") or 0),
            detected_at=float(raw.get("detected_at") or 0),
            baseline_frozen=float(raw.get("baseline_frozen") or 0),
            source_rvol_at_detection=float(raw.get("source_rvol_at_detection") or 0),
            expires_at=float(raw.get("expires_at") or 0),
            state=str(raw.get("state") or STATE_OBSERVING),
            consumed_at=(
                float(raw["consumed_at"]) if raw.get("consumed_at") is not None else None
            ),
            invalidation_reason=raw.get("invalidation_reason"),
            reserved_order_id=raw.get("reserved_order_id"),
            reserved_at=(
                float(raw["reserved_at"]) if raw.get("reserved_at") is not None else None
            ),
            origin_detected_bars=list(raw.get("origin_detected_bars") or []),
        )
        return ev

    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES


class SurgeEventStore:
    """Per-symbol at most one unconsumed active event (OBSERVING/RESERVED)."""

    def __init__(self) -> None:
        self.by_symbol: dict[str, SurgeEvent] = {}
        # history of terminal events for audit (bounded)
        self.history: list[dict[str, Any]] = []
        self._history_cap = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "by_symbol": {k: v.to_dict() for k, v in self.by_symbol.items()},
            "history": list(self.history[-self._history_cap :]),
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "SurgeEventStore":
        store = cls()
        if not d:
            return store
        for sym, evd in (d.get("by_symbol") or {}).items():
            store.by_symbol[str(sym)] = SurgeEvent.from_dict(evd)
        store.history = list(d.get("history") or [])[-store._history_cap :]
        return store

    def get(self, symbol: str) -> Optional[SurgeEvent]:
        return self.by_symbol.get(symbol)

    def _archive(self, ev: SurgeEvent) -> None:
        self.history.append(ev.to_dict())
        if len(self.history) > self._history_cap:
            self.history = self.history[-self._history_cap :]

    def invalidate(
        self, symbol: str, *, reason: str, now: float, only_if_active: bool = True
    ) -> Optional[SurgeEvent]:
        ev = self.by_symbol.get(symbol)
        if not ev:
            return None
        if only_if_active and not ev.is_active():
            return ev
        if ev.state == STATE_RESERVED:
            # keep reserved occupancy if order unclear — caller must reconcile;
            # Top15 drop still invalidates for NEW opens but order cancel is separate
            pass
        ev.state = STATE_INVALID
        ev.invalidation_reason = reason
        ev.consumed_at = None
        self._archive(ev)
        # remove from active map so it cannot be reused
        self.by_symbol.pop(symbol, None)
        return ev

    def expire_if_needed(self, symbol: str, *, now: float) -> Optional[SurgeEvent]:
        ev = self.by_symbol.get(symbol)
        if not ev or not ev.is_active():
            return ev
        if float(now) >= float(ev.expires_at):
            # RESERVED past expire: still expire eligibility; order path must reconcile
            ev.state = STATE_EXPIRED
            ev.invalidation_reason = "WINDOW_EXPIRED"
            self._archive(ev)
            self.by_symbol.pop(symbol, None)
            return ev
        return ev

    def try_create_origin(
        self,
        *,
        symbol: str,
        source_bar_open_time: float,
        detected_at: float,
        baseline_frozen: float,
        source_rvol: float,
        has_position: bool,
        has_pending_entry: bool,
    ) -> tuple[Optional[SurgeEvent], str]:
        """Create/keep origin surge event. Same source bar must not reset expires_at.

        While in position: do not store events (spec §六).
        """
        if has_position:
            return None, "SKIP_STORE_WHILE_IN_POSITION"
        if baseline_frozen is None or float(baseline_frozen) <= 0:
            return None, "BASELINE_INVALID"
        T = float(source_bar_open_time)
        existing = self.by_symbol.get(symbol)

        if existing and existing.is_active():
            if abs(float(existing.source_bar_open_time) - T) < 1e-9:
                # same source bar — do not reset expires; mark bar as detected
                if T not in existing.origin_detected_bars:
                    existing.origin_detected_bars.append(T)
                return existing, "SAME_SOURCE_BAR_KEEP"
            # new source bar while old active
            if has_pending_entry or existing.state == STATE_RESERVED:
                return existing, "BLOCKED_BY_PENDING_OR_RESERVED"
            # supersede
            existing.state = STATE_SUPERSEDED
            existing.invalidation_reason = "SUPERSEDED_BY_NEW_ORIGIN"
            self._archive(existing)
            self.by_symbol.pop(symbol, None)

        # new event — independent source + baseline
        ev = SurgeEvent(
            event_id=f"se_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            source_bar_open_time=T,
            detected_at=float(detected_at),
            baseline_frozen=float(baseline_frozen),
            source_rvol_at_detection=float(source_rvol),
            expires_at=expires_at_for(T),
            state=STATE_OBSERVING,
            origin_detected_bars=[T],
        )
        self.by_symbol[symbol] = ev
        return ev, "CREATED"

    def reserve(
        self, symbol: str, *, order_id: str, now: float
    ) -> tuple[bool, str, Optional[SurgeEvent]]:
        ev = self.by_symbol.get(symbol)
        if not ev:
            return False, "NO_EVENT", None
        if ev.state == STATE_RESERVED:
            if ev.reserved_order_id == order_id:
                return True, "ALREADY_RESERVED_SAME", ev
            return False, "ALREADY_RESERVED_OTHER", ev
        if ev.state != STATE_OBSERVING:
            return False, f"BAD_STATE:{ev.state}", ev
        if float(now) >= float(ev.expires_at):
            self.expire_if_needed(symbol, now=now)
            return False, "EXPIRED", None
        ev.state = STATE_RESERVED
        ev.reserved_order_id = order_id
        ev.reserved_at = float(now)
        return True, "OK", ev

    def consume_on_fill(
        self, symbol: str, *, now: float, order_id: Optional[str] = None
    ) -> Optional[SurgeEvent]:
        """Any first-leg fill (incl. partial) consumes the event."""
        ev = self.by_symbol.get(symbol)
        if not ev:
            return None
        if order_id and ev.reserved_order_id and ev.reserved_order_id != order_id:
            # fill for different order — still consume active event for symbol first entry
            pass
        if ev.state in TERMINAL_STATES:
            return ev
        ev.state = STATE_CONSUMED
        ev.consumed_at = float(now)
        ev.invalidation_reason = "FIRST_FILL"
        self._archive(ev)
        self.by_symbol.pop(symbol, None)
        return ev

    def release_on_zero_fill_cancel(
        self, symbol: str, *, now: float, order_id: Optional[str] = None
    ) -> tuple[Optional[SurgeEvent], str]:
        """Confirmed zero-fill cancel → return to OBSERVING if not expired."""
        ev = self.by_symbol.get(symbol)
        if not ev:
            return None, "NO_EVENT"
        if ev.state != STATE_RESERVED:
            return ev, f"NOT_RESERVED:{ev.state}"
        if order_id and ev.reserved_order_id and ev.reserved_order_id != order_id:
            return ev, "ORDER_ID_MISMATCH_KEEP_RESERVED"
        if float(now) >= float(ev.expires_at):
            ev.state = STATE_EXPIRED
            ev.invalidation_reason = "WINDOW_EXPIRED_ON_RELEASE"
            ev.reserved_order_id = None
            self._archive(ev)
            self.by_symbol.pop(symbol, None)
            return None, "EXPIRED"
        ev.state = STATE_OBSERVING
        ev.reserved_order_id = None
        ev.reserved_at = None
        return ev, "RESTORED_OBSERVING"

    def validate_after_restart(self, *, now: float) -> list[dict[str, Any]]:
        """Expire/invalidate events that cannot be proven valid after restart."""
        notes: list[dict[str, Any]] = []
        for sym in list(self.by_symbol.keys()):
            ev = self.by_symbol[sym]
            if float(now) >= float(ev.expires_at):
                self.expire_if_needed(sym, now=now)
                notes.append({"symbol": sym, "action": "EXPIRED_ON_RESTART"})
                continue
            if ev.state == STATE_RESERVED:
                # cannot prove fill/cancel without order reconcile — keep RESERVED
                notes.append(
                    {
                        "symbol": sym,
                        "action": "KEEP_RESERVED_NEEDS_ORDER_RECONCILE",
                        "order_id": ev.reserved_order_id,
                    }
                )
                continue
            if ev.state == STATE_OBSERVING:
                if ev.baseline_frozen <= 0 or not ev.event_id:
                    self.invalidate(sym, reason="RESTART_INCOMPLETE", now=now)
                    notes.append({"symbol": sym, "action": "INVALID_INCOMPLETE"})
        return notes
