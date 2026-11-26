"""H price_action_v2 entry signals: independent paths A/B/C.

Legacy evaluate_entry_v1 kept for cont_arm offline rollback tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .bars import Bar3m
from .entry_gate import EntryGateStore, SymbolEntryGate
from .path_abc import (
    evaluate_chop,
    evaluate_path_a_surge,
    evaluate_path_b_continuation,
    evaluate_path_c_reversal,
)
from .price_series import SparseSecSeries
from .surge_events import SurgeEvent

# ---------------------------------------------------------------------------
# Shared decision type
# ---------------------------------------------------------------------------


@dataclass
class SignalDecision:
    ok: bool
    side: Optional[str] = None  # LONG / SHORT
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    entry_path: Optional[str] = None
    event_id: Optional[str] = None
    candidate_id: Optional[str] = None
    paths: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# v2 evaluate
# ---------------------------------------------------------------------------


def evaluate_entry(
    *,
    decision_ts: float,
    bar: Optional[Bar3m],
    baseline_median: Optional[float],
    prices_60: list[float],
    prices_15: list[float],
    last_trade_ts: float,
    gap_unfilled: bool,
    has_position: bool,
    has_exit_pending: bool,
    first_entry_already_sent_this_bar: bool = False,  # ignored in v2 (kept for API compat)
    surge_event: Optional[SurgeEvent] = None,
    sparse: Optional[SparseSecSeries] = None,
    gate: Optional[SymbolEntryGate] = None,
    tick_size: float = 0.0001,
    position_side: Optional[str] = None,
    has_pending_entry: bool = False,
    flat_confirmed: bool = True,
) -> SignalDecision:
    """v2 first-entry: A∪B∪C; NO_SURGE does not block B/C."""
    m: dict[str, Any] = {
        "decision_ts": decision_ts,
        "strategy_version": config.STRATEGY_VERSION,
        "config_hash": config.config_hash(),
        "eval_sec": None,
    }
    paths_out: dict[str, Any] = {}

    # --- Common gates ---
    if has_exit_pending:
        return SignalDecision(False, reason="EXIT_PENDING", metrics=m, paths=paths_out)
    if gap_unfilled:
        return SignalDecision(False, reason="TRADE_STREAM_GAP", metrics=m, paths=paths_out)
    stale_lim = min(float(config.TRADE_STALE_SEC), float(config.V2_MAX_TRADE_AGE_SEC))
    if last_trade_ts <= 0 or (decision_ts - last_trade_ts) > stale_lim:
        return SignalDecision(False, reason="TRADE_STALE", metrics=m, paths=paths_out)

    if sparse is None:
        return SignalDecision(False, reason="SPARSE_SERIES_MISSING", metrics=m, paths=paths_out)

    t = sparse.latest_complete_sec(decision_ts)
    m["eval_sec"] = t
    if not sparse.has(t):
        return SignalDecision(
            False, reason="DATA_INSUFFICIENT", metrics=m, paths=paths_out
        )

    # --- Path A (SURGE) — failure does not stop B/C ---
    a = evaluate_path_a_surge(
        decision_ts=decision_ts,
        bar=bar,
        baseline_median=baseline_median,
        dense_prices_60=prices_60,
        dense_prices_15=prices_15,
    )
    paths_out["SURGE"] = a.to_dict()

    # Optional legacy cont_arm (default OFF) — never a 4th concurrent path when A/B/C already decide
    if (
        config.ENABLE_CONTINUATION_ARM
        and surge_event is not None
        and not a.ok
    ):
        # kept for rollback; evaluated via v1 helper only when explicitly enabled
        from .signals_v1_legacy import evaluate_continuation_only

        cont = evaluate_continuation_only(
            decision_ts=decision_ts,
            bar=bar,
            prices_60=prices_60,
            prices_15=prices_15,
            has_position=has_position,
            surge_event=surge_event,
        )
        paths_out["CONTINUATION_ARM_LEGACY"] = cont.to_dict() if cont else None

    # --- Path B ---
    b = evaluate_path_b_continuation(sparse, t)
    g = gate or SymbolEntryGate()
    if b.ok and b.side:
        if g.b_streak_side == b.side and g.b_streak_count > 0:
            expected = int(g.b_streak_since or 0) + int(g.b_streak_count)
            if t == expected:
                g.b_streak_count += 1
            else:
                g.b_streak_side = b.side
                g.b_streak_since = t
                g.b_streak_count = 1
        else:
            g.b_streak_side = b.side
            g.b_streak_since = t
            g.b_streak_count = 1
        b.detail["streak"] = g.b_streak_count
        b.detail["streak_since"] = g.b_streak_since
        if g.b_streak_count < int(config.V2_B_STREAK_SEC):
            b.ok = False
            b.reason = "CONTINUATION_STREAK_SHORT"
        else:
            b.ok = True
            b.reason = "OK"
    else:
        g.b_streak_side = None
        g.b_streak_count = 0
        g.b_streak_since = None
    paths_out["CONTINUATION"] = b.to_dict()

    # --- Path C ---
    c, new_arm = evaluate_path_c_reversal(
        sparse, t, tick_size=tick_size, armed=g.reversal_arm
    )
    g.reversal_arm = new_arm
    paths_out["REVERSAL"] = c.to_dict()

    # Merge passing path sides
    passing: list[tuple[str, str]] = []  # (path, side)
    for pr in (a, b, c):
        if pr.ok and pr.side:
            passing.append((pr.path, pr.side))

    # Shared metrics snapshot (nulls when unavailable)
    m.update(
        {
            "RVOL": (a.detail or {}).get("RVOL"),
            "ret_10": (c.detail or {}).get("ret_10"),
            "ret_15": (b.detail or {}).get("ret_15"),
            "ret_60": (b.detail or {}).get("ret_60"),
            "ER_10": (c.detail or {}).get("ER_10"),
            "ER_30": (b.detail or {}).get("ER_30"),
            "CLV60": (b.detail or {}).get("CLV60"),
            "ER_60_dense": (a.detail or {}).get("ER_60_dense"),
            "reversal_arm": g.reversal_arm,
            "b_streak": {"side": g.b_streak_side, "count": g.b_streak_count, "since": g.b_streak_since},
        }
    )

    if not passing:
        # clear must_clear when no path passes
        if g.must_clear_before_rearm:
            g.must_clear_before_rearm = False
        reasons = [f"{k}:{paths_out[k].get('reason')}" for k in ("SURGE", "CONTINUATION", "REVERSAL") if k in paths_out]
        return SignalDecision(
            False,
            reason="NO_PATH_PASS",
            metrics=m,
            paths=paths_out,
            tags=reasons,
        )

    sides = {s for _, s in passing}
    if len(sides) > 1:
        return SignalDecision(
            False,
            reason="DIRECTION_CONFLICT",
            metrics=m,
            paths=paths_out,
            tags=[f"{p}:{s}" for p, s in passing],
        )

    side = next(iter(sides))
    tags = [p for p, s in passing if s == side]

    # CHOP gate for B/C contributions
    bc_tags = [p for p in tags if p in ("CONTINUATION", "REVERSAL")]
    if bc_tags:
        chop = evaluate_chop(sparse, t, side=side, tick_size=tick_size)
        m["chop"] = chop
        if chop.get("chop") and not chop.get("escape_ok"):
            # drop B/C; keep A if present
            tags = [p for p in tags if p == "SURGE"]
            if not tags:
                return SignalDecision(
                    False,
                    reason="CHOP_BLOCK",
                    metrics=m,
                    paths=paths_out,
                    tags=[f"CHOP flips={chop.get('flip_count')}"],
                )
        elif chop.get("chop") and chop.get("escape_ok"):
            m["chop_escape"] = True

    # Position / opposite / pending
    if has_pending_entry:
        return SignalDecision(
            False,
            side=side,
            tags=tags,
            reason="ENTRY_ORDER_ALREADY_PENDING",
            metrics=m,
            paths=paths_out,
        )

    if has_position:
        pos_side = str(position_side or "").upper()
        if pos_side == side:
            return SignalDecision(
                False,
                side=side,
                tags=tags,
                reason="ALREADY_IN_POSITION",  # record only; adds use 0.5% rule
                metrics=m,
                paths=paths_out,
            )
        return SignalDecision(
            False,
            side=side,
            tags=tags,
            reason="OPPOSITE_POSITION",
            metrics=m,
            paths=paths_out,
        )

    if not flat_confirmed:
        return SignalDecision(
            False,
            side=side,
            tags=tags,
            reason="FLAT_NOT_CONFIRMED",
            metrics=m,
            paths=paths_out,
        )

    # Consume / re-arm rules
    if g.must_clear_before_rearm:
        return SignalDecision(
            False,
            side=side,
            tags=tags,
            reason="CANDIDATE_MUST_CLEAR_BEFORE_REARM",
            metrics=m,
            paths=paths_out,
        )

    # Cooldown after exit
    if g.last_exit_ts > 0 and (decision_ts - g.last_exit_ts) < float(
        config.V2_COOLDOWN_AFTER_EXIT_SEC
    ):
        return SignalDecision(
            False,
            side=side,
            tags=tags,
            reason="COOLDOWN_AFTER_EXIT",
            metrics=m,
            paths=paths_out,
        )

    # Rate limit attempts / 180s
    cut = decision_ts - 180.0
    n_att = sum(1 for x in g.first_entry_attempts if x >= cut)
    m["first_entry_attempts_180s"] = n_att
    if n_att >= int(config.V2_MAX_FIRST_ENTRY_ATTEMPTS_180S):
        return SignalDecision(
            False,
            side=side,
            tags=tags,
            reason="FIRST_ENTRY_RATE_LIMIT_180S",
            metrics=m,
            paths=paths_out,
        )

    # Candidate id
    cand_id = g.candidate.get("id") if g.candidate else None
    if not cand_id:
        cand_id = f"cand_{t}_{side}_{'_'.join(tags)}"
        g.candidate = {
            "id": cand_id,
            "side": side,
            "tags": tags,
            "first_sec": t,
            "confirmed_sec": t,
        }
    else:
        g.candidate["confirmed_sec"] = t
        g.candidate["tags"] = tags

    m["candidate"] = dict(g.candidate)

    # Prefer SURGE path label when present else CONTINUATION else REVERSAL
    if "SURGE" in tags:
        entry_path = "SURGE"
    elif "CONTINUATION" in tags:
        entry_path = "CONTINUATION"
    else:
        entry_path = "REVERSAL"

    return SignalDecision(
        True,
        side=side,
        tags=tags,
        reason="OK",
        metrics=m,
        paths=paths_out,
        entry_path=entry_path,
        candidate_id=cand_id,
        event_id=surge_event.event_id if surge_event is not None else None,
    )


def mark_candidate_consumed(gate: SymbolEntryGate, candidate_id: Optional[str], side: str) -> None:
    gate.last_consumed_id = candidate_id
    gate.last_consumed_side = side
    gate.must_clear_before_rearm = True
    gate.candidate = None
    gate.b_streak_side = None
    gate.b_streak_count = 0
    gate.b_streak_since = None
    gate.reversal_arm = None


# ---------------------------------------------------------------------------
# Legacy v1 (import shim) — full body moved to signals_v1_legacy for cont_arm tests
# ---------------------------------------------------------------------------


def evaluate_entry_v1(**kwargs: Any) -> SignalDecision:
    from .signals_v1_legacy import evaluate_entry as _v1

    return _v1(**kwargs)
