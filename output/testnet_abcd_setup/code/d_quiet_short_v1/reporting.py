"""Settlement / review helpers for D (reports do not force flatten)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.settlement_apply import read_settlement_fills_report

from . import config
from .util import atomic_write_json, now_iso


def write_pending_confirmations() -> Path:
    paths = config.paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    payload = {
        "at": now_iso(),
        "strategy_version": config.STRATEGY_VERSION,
        "confirmed_magnitudes": {
            "stake_usdt": config.STAKE_USDT,
            "tp_usdt": config.TP_USDT,
            "sl_usdt": config.SL_USDT,
            "max_slots": config.MAX_CONCURRENT_SLOTS,
        },
        "pending": config.pending_confirmations(),
        "paper_fills_allowed": config.paper_fills_allowed(),
        "note": "Do not start paper fills until capital + depth are confirmed.",
    }
    atomic_write_json(paths["pending"], payload)
    return paths["pending"]


def write_review_stub() -> Path:
    paths = config.paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    text = f"""# D quiet-short review stub

- version: `{config.STRATEGY_VERSION}`
- updated: {now_iso()}
- paper fills allowed: `{config.paper_fills_allowed()}`

## Pending confirmations
```json
{json.dumps(config.pending_confirmations(), ensure_ascii=False, indent=2)}
```

## Account-level drawdown
未完成完整帳戶權益序列回撤驗證（標註）。

## Fill model
- entry: limit touch when bid≥limit; fill at limit (remainder model; not guaranteed by depth)
- exit: aggressive cover at ask×(1+slip); mid never used as fill
- fees: {config.FEE_RATE_ONE_SIDE} one side; slip {config.SLIPPAGE_ADVERSE}; no maker rebate
"""
    paths["review"].write_text(text, encoding="utf-8")
    return paths["review"]


def snapshot_settlement(tag: str, account: dict[str, Any], *, fills_path: Path | str | None = None) -> Path:
    """Formal D settlement snapshot entry.

    Call chain:
      snapshot_settlement → read_settlement_fills_report → scan + dedupe
    Account fields are copied as-is (not recomputed from fills).
    """
    paths = config.paths()
    fill_p = Path(fills_path) if fills_path else paths["fills"]
    fills_report = read_settlement_fills_report(fill_p)
    out = paths["settlement"] / f"snapshot_{tag}.json"
    payload = {
        "tag": tag,
        "at": now_iso(),
        "strategy_version": config.STRATEGY_VERSION,
        "account": {
            "realized_net_u": account.get("realized_net_u"),
            "realized_gross_u": account.get("realized_gross_u"),
            "realized_fees_u": account.get("realized_fees_u"),
            "cash_u": account.get("cash_u"),
            "npos": len(account.get("positions") or {}),
            "npending": len(account.get("pending_limit_entries") or {}),
        },
        "settlement_fills_report": fills_report,
        "complete_confirmed": bool(fills_report.get("complete_confirmed")),
        "note": (
            "Settlement snapshot only — does not flatten positions. "
            "Account cash is authoritative; fills re-read is report-only."
        ),
        "account_drawdown": "NOT_COMPUTED",
    }
    if fills_report.get("conflicts") or fills_report.get("incomplete"):
        payload["complete_confirmed"] = False
        payload["note"] += " CONFLICT_OR_INCOMPLETE — not full confirmed totals."
    atomic_write_json(out, payload)
    return out
