"""First-entry gating for h_price_action_v2 (cooldown / consume / rate limit).

Does NOT block protective exits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SymbolEntryGate:
    last_exit_ts: float = 0.0
    last_flat_confirmed_ts: float = 0.0
    # attempts: list of unix ts for first-entry order tries (incl. cancel/reject)
    first_entry_attempts: list[float] = field(default_factory=list)
    # active candidate waiting for streak / confirm
    candidate: Optional[dict[str, Any]] = None
    # last consumed candidate id (cannot re-fire while still continuously true)
    last_consumed_id: Optional[str] = None
    last_consumed_side: Optional[str] = None
    # after consume, must see failure before new candidate of same continuous signal
    must_clear_before_rearm: bool = False
    # reversal arm state (path C)
    reversal_arm: Optional[dict[str, Any]] = None
    # B streak: consecutive seconds path B ok for side
    b_streak_side: Optional[str] = None
    b_streak_count: int = 0
    b_streak_since: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_exit_ts": self.last_exit_ts,
            "last_flat_confirmed_ts": self.last_flat_confirmed_ts,
            "first_entry_attempts": list(self.first_entry_attempts),
            "candidate": self.candidate,
            "last_consumed_id": self.last_consumed_id,
            "last_consumed_side": self.last_consumed_side,
            "must_clear_before_rearm": self.must_clear_before_rearm,
            "reversal_arm": self.reversal_arm,
            "b_streak_side": self.b_streak_side,
            "b_streak_count": self.b_streak_count,
            "b_streak_since": self.b_streak_since,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "SymbolEntryGate":
        obj = cls()
        if not d:
            return obj
        for k, v in d.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj


@dataclass
class EntryGateStore:
    symbols: dict[str, SymbolEntryGate] = field(default_factory=dict)

    def get(self, symbol: str) -> SymbolEntryGate:
        sym = symbol.upper()
        g = self.symbols.get(sym)
        if g is None:
            g = SymbolEntryGate()
            self.symbols[sym] = g
        return g

    def to_dict(self) -> dict[str, Any]:
        return {k: v.to_dict() for k, v in self.symbols.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "EntryGateStore":
        store = cls()
        if not d:
            return store
        for k, v in d.items():
            store.symbols[str(k).upper()] = SymbolEntryGate.from_dict(v)
        return store

    def note_exit(self, symbol: str, ts: float) -> None:
        g = self.get(symbol)
        g.last_exit_ts = float(ts)

    def note_flat_confirmed(self, symbol: str, ts: float) -> None:
        g = self.get(symbol)
        g.last_flat_confirmed_ts = float(ts)

    def note_attempt(self, symbol: str, ts: float) -> None:
        g = self.get(symbol)
        g.first_entry_attempts.append(float(ts))
        # trim old
        cut = float(ts) - 600.0
        g.first_entry_attempts = [x for x in g.first_entry_attempts if x >= cut]

    def attempts_in_window(self, symbol: str, now: float, window: float) -> int:
        g = self.get(symbol)
        lo = float(now) - float(window)
        return sum(1 for x in g.first_entry_attempts if x >= lo)

    def cooldown_active(self, symbol: str, now: float, cooldown_sec: float) -> bool:
        g = self.get(symbol)
        if g.last_exit_ts <= 0:
            return False
        return (float(now) - float(g.last_exit_ts)) < float(cooldown_sec)

    def new_candidate_id(self) -> str:
        return f"cand_{uuid.uuid4().hex[:12]}"


_STORE: Optional[EntryGateStore] = None


def get_entry_gate_store(raw: dict[str, Any] | None = None) -> EntryGateStore:
    global _STORE
    if _STORE is None:
        _STORE = EntryGateStore.from_dict(raw or {})
    return _STORE


def reset_entry_gate_store_for_tests() -> None:
    global _STORE
    _STORE = None


def sync_entry_gate_to_account(account_dict_field: dict[str, Any], store: EntryGateStore) -> None:
    account_dict_field.clear()
    account_dict_field.update(store.to_dict())
