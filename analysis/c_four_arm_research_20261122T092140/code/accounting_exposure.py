"""Accounting formulas + exposure-time integral for research four-arm Paper.

Formulas (explicit):
- realized_net_u on account already nets fees (gross − fees on closes; entry fees reduce realized).
  Do NOT subtract fees again when composing equity from realized.
- cash_u = free cash (margin locked out of cash on open/add; released on close).
- used_margin = sum(position.margin_u)
- wallet_balance = initial_capital + realized_net_u
- floating_unrealized = sum over positions of mark PnL − estimated exit fee (estimate_net_unrealized)
- equity = initial_capital + realized_net_u + floating_unrealized
  (= cash + used_margin + floating_unrealized when cash identity holds)
- Funding: ALWAYS separate column; UNKNOWN must not become 0.

Exposure integral:
- Method: trapezoid / right-riemann on |gross_notional| between snapshots
- Unit: U·seconds (also report /3600 as U·hours)
- Sources: snapshot ts + mark source recorded
- Restart: store last_integral_ts; do not double-count overlapping intervals
- Gaps: coverage_ratio + UNKNOWN intervals listed
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ExposureIntegralState:
    integral_u_sec: float = 0.0
    last_ts: Optional[float] = None
    last_gross_notional: float = 0.0
    gap_intervals: list[dict[str, Any]] = field(default_factory=list)
    covered_sec: float = 0.0
    wall_sec: float = 0.0
    max_gap_sec_before_unknown: float = 120.0

    def update(
        self,
        *,
        now_ts: float,
        gross_notional_u: float,
        price_source: str,
    ) -> dict[str, Any]:
        if self.last_ts is None:
            self.last_ts = now_ts
            self.last_gross_notional = float(gross_notional_u)
            return {
                "delta_u_sec": 0.0,
                "integral_u_sec": self.integral_u_sec,
                "gap": False,
            }
        dt = float(now_ts) - float(self.last_ts)
        if dt < 0:
            # clock skew / restart replay — skip negative
            self.last_ts = now_ts
            self.last_gross_notional = float(gross_notional_u)
            return {"delta_u_sec": 0.0, "integral_u_sec": self.integral_u_sec, "gap": False, "note": "negative_dt_skipped"}
        self.wall_sec += dt
        if dt > self.max_gap_sec_before_unknown:
            self.gap_intervals.append(
                {
                    "from_ts": self.last_ts,
                    "to_ts": now_ts,
                    "dt_sec": dt,
                    "status": "UNKNOWN_GAP",
                    "price_source": price_source,
                }
            )
            # do not invent exposure across large gaps
            delta = 0.0
            gap = True
        else:
            # right-riemann using previous notional over dt
            delta = abs(float(self.last_gross_notional)) * dt
            self.integral_u_sec += delta
            self.covered_sec += dt
            gap = False
        self.last_ts = now_ts
        self.last_gross_notional = float(gross_notional_u)
        return {
            "delta_u_sec": delta,
            "integral_u_sec": self.integral_u_sec,
            "integral_u_hours": self.integral_u_sec / 3600.0,
            "gap": gap,
            "coverage_ratio": (self.covered_sec / self.wall_sec) if self.wall_sec > 0 else None,
        }


def position_marks(st: Any, books: dict[str, dict], *, mark_fn) -> list[dict[str, Any]]:
    from breakout_follow_v1.account import Position
    from breakout_follow_v1.portfolio import estimate_net_unrealized

    rows = []
    for sym, pdct in list(st.positions.items()):
        pos = Position.from_dict(pdct)
        b = books.get(sym)
        if not b:
            rows.append(
                {
                    "symbol": sym,
                    "qty": pos.qty_remaining,
                    "mark": None,
                    "unrealized_u": "UNKNOWN",
                    "mark_source": "MISSING_QUOTE",
                }
            )
            continue
        mark = mark_fn(pos.side, b)
        u = estimate_net_unrealized(pos, mark)
        rows.append(
            {
                "symbol": sym,
                "side": pos.side,
                "qty": pos.qty_remaining,
                "entry": pos.entry_price,
                "margin_u": pos.margin_u,
                "mark": mark,
                "unrealized_u": u,
                "mark_source": f"bookTicker:{b.get('fetched_at')}",
                "bid": b.get("bid"),
                "ask": b.get("ask"),
                "quote_age_sec": (time.time() - float(b["fetched_at"])) if b.get("fetched_at") else None,
            }
        )
    return rows


def snapshot_account(
    st: Any,
    books: dict[str, dict],
    *,
    mark_fn,
    exposure: ExposureIntegralState,
    event_id: str | None = None,
    reason: str = "SNAPSHOT",
) -> dict[str, Any]:
    marks = position_marks(st, books, mark_fn=mark_fn)
    floating = 0.0
    unknown_u = False
    gross_notional = 0.0
    for m in marks:
        if m["unrealized_u"] == "UNKNOWN":
            unknown_u = True
        else:
            floating += float(m["unrealized_u"])
        if m.get("mark") is not None:
            gross_notional += abs(float(m["mark"]) * float(m["qty"]))
    used_margin = float(st.used_margin())
    cash = float(st.cash_u)
    realized = float(st.realized_net_u)
    fees = float(st.realized_fees_u)
    initial = float(st.initial_capital_u)
    equity = initial + realized + (None if unknown_u else floating)
    # identity check when marks complete
    identity_cash = cash + used_margin
    identity_wallet = initial + realized
    now_ts = time.time()
    exp = exposure.update(now_ts=now_ts, gross_notional_u=gross_notional, price_source=reason)
    row = {
        "ts_unix": now_ts,
        "event_id": event_id,
        "reason": reason,
        "arm": getattr(st, "research_arm", None) or getattr(st, "profile_id", None),
        "run_id": st.run_id,
        "cash_u": cash,
        "margin_u": used_margin,
        "realized_net_u": realized,
        "realized_fees_u": fees,
        "funding_u": "UNKNOWN" if getattr(st, "funding_data_gap", True) else float(getattr(st, "realized_funding_u", 0.0)),
        "funding_note": "UNKNOWN_not_default_zero" if getattr(st, "funding_data_gap", True) else "recorded",
        "positions": marks,
        "floating_unrealized_u": "UNKNOWN" if unknown_u else floating,
        "equity_u": "UNKNOWN" if equity is None else equity,
        "gross_notional_u": gross_notional,
        "net_notional_u": gross_notional,  # flat directional sum of abs; signed net optional
        "equity_formula": "initial + realized_net_u + floating_unrealized (fees already inside realized)",
        "cash_identity_u": identity_cash,
        "wallet_balance_u": identity_wallet,
        "exposure_integral_u_sec": exp["integral_u_sec"],
        "exposure_integral_u_hours": exp.get("integral_u_hours"),
        "exposure_coverage_ratio": exp.get("coverage_ratio"),
        "exposure_gap": exp.get("gap"),
        "ledger_write_failed": bool(getattr(st, "ledger_write_failed", False)),
    }
    return row


def append_snapshot(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
