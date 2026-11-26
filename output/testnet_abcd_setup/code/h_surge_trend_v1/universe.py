"""Universe exclusion + ranking + Top10/Top15 tracking (pure + snapshot helpers)."""

from __future__ import annotations

from typing import Any, Optional

from . import config


def is_excluded(symbol: str, base: str, info: dict[str, Any]) -> tuple[bool, str]:
    """Exclude by exact base match / typed fields. No substring false-positives on base lists."""
    if info.get("contractType") != "PERPETUAL":
        return True, "NOT_PERPETUAL"
    if info.get("quoteAsset") != "USDT":
        return True, "NOT_USDT"
    if info.get("status") != "TRADING":
        return True, "NOT_TRADING"
    sym = symbol.upper()
    base_u = base.upper()
    for suf in config.LEVERAGE_TOKEN_SUFFIXES:
        if sym.endswith(suf):
            return True, "LEVERAGE_TOKEN"
    if base_u in config.STABLE_BASES:
        return True, "STABLE_BASE"
    if base_u in config.EXCLUDED_BASE_ASSETS:
        return True, "EXCLUDED_BASE_ASSET"
    if base_u in config.WRAPPED_STAKED_BASES:
        return True, "WRAPPED_OR_STAKED_BASE"
    ut = str(info.get("underlyingType") or "").upper()
    if ut in config.NON_CRYPTO_UNDERLYING_TYPES:
        return True, f"UNDERLYING_TYPE:{ut}"
    subtypes = info.get("underlyingSubType") or []
    items = subtypes if isinstance(subtypes, list) else [subtypes]
    for item in items:
        text = str(item)
        for kw in config.NON_CRYPTO_SUBTYPE_KEYWORDS:
            if kw.lower() in text.lower():
                return True, f"SUBTYPE:{kw}"
    if base_u in config.TRADFI_BASE_BLACKLIST:
        return True, "TRADFI_BASE_MAP"
    for prefix in config.TRADFI_SYMBOL_PREFIXES:
        # prefix map is on symbol/base start — documented secondary map
        if sym.startswith(prefix) or base_u.startswith(prefix):
            return True, "TRADFI_PREFIX_MAP"
    # Classification unknown: missing/empty underlyingType and no crypto confirmation
    if ut not in ("COIN", "CRYPTO") and ut != "":
        # already handled non-crypto above; other unknown labels → exclude
        if ut not in config.CRYPTO_UNDERLYING_TYPES:
            return True, f"CLASSIFICATION_UNKNOWN:{ut or 'EMPTY'}"
    if ut == "":
        # Binance often omits; treat as unknown → exclude & record (spec: 分類不明先排除)
        return True, "CLASSIFICATION_UNKNOWN:EMPTY_UNDERLYING_TYPE"
    return False, ""


def lot_filters(sym_info: dict[str, Any]) -> dict[str, Any]:
    step = 0.001
    min_qty = 0.001
    min_notional = 5.0
    tick = 0.0001
    for f in sym_info.get("filters") or []:
        t = f.get("filterType")
        if t == "LOT_SIZE":
            step = float(f.get("stepSize") or step)
            min_qty = float(f.get("minQty") or min_qty)
        elif t == "MIN_NOTIONAL":
            min_notional = float(f.get("notional") or f.get("minNotional") or min_notional)
        elif t == "PRICE_FILTER":
            tick = float(f.get("tickSize") or tick)
    return {"stepSize": step, "minQty": min_qty, "minNotional": min_notional, "tickSize": tick}


def rank_by_24h_change(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort desc by 24h pct; tie-break symbol asc. Assign rank 1..n. No future data."""
    ordered = sorted(
        rows,
        key=lambda r: (-float(r.get("price_change_pct_24h") or 0.0), str(r.get("symbol") or "")),
    )
    out = []
    for i, r in enumerate(ordered, start=1):
        row = dict(r)
        row["rank"] = i
        out.append(row)
    return out


def update_tracking(
    prev_tracked: dict[str, dict[str, Any]],
    ranked: list[dict[str, Any]],
    *,
    top_enter: int = config.TOP_ENTER,
    top_keep: int = config.TOP_KEEP,
    now_ts: float,
) -> dict[str, Any]:
    """Return new tracked set + events.

    Rules:
    - rank<=top_enter → enter/keep tracking (signal-eligible)
    - already tracked and rank<=top_keep → keep tracking
    - rank>top_keep → drop from tracking (stop new/add; cancel pending); positions remain managed elsewhere
    - re-enter signals only when back in top_enter
    """
    by_sym = {r["symbol"]: r for r in ranked}
    new_tracked: dict[str, dict[str, Any]] = {}
    entered: list[str] = []
    kept: list[str] = []
    dropped: list[str] = []

    # keep / drop existing
    for sym, meta in prev_tracked.items():
        r = by_sym.get(sym)
        rank = int(r["rank"]) if r else 10**9
        if rank <= top_keep:
            row = {
                **meta,
                "rank": rank,
                "price_change_pct_24h": (r or {}).get("price_change_pct_24h"),
                "signal_eligible": rank <= top_enter,
                "updated_ts": now_ts,
            }
            # if was dropped before and now back in top_enter → restore signals
            if rank <= top_enter:
                row["signal_eligible"] = True
            else:
                row["signal_eligible"] = False  # in keep band but not top_enter
            new_tracked[sym] = row
            kept.append(sym)
        else:
            dropped.append(sym)

    # enter new from top_enter
    for r in ranked:
        if int(r["rank"]) > top_enter:
            break
        sym = r["symbol"]
        if sym in new_tracked:
            new_tracked[sym]["signal_eligible"] = True
            new_tracked[sym]["rank"] = int(r["rank"])
            continue
        new_tracked[sym] = {
            "symbol": sym,
            "rank": int(r["rank"]),
            "price_change_pct_24h": r.get("price_change_pct_24h"),
            "entered_ts": now_ts,
            "updated_ts": now_ts,
            "signal_eligible": True,
        }
        entered.append(sym)

    return {
        "tracked": new_tracked,
        "entered": entered,
        "kept": kept,
        "dropped": dropped,
        "top5": [r["symbol"] for r in ranked if int(r["rank"]) <= top_enter],
        "top15": [r["symbol"] for r in ranked if int(r["rank"]) <= top_keep],
    }


def rank_is_fresh(last_rank_ts: Optional[float], now_ts: float, stale_sec: float) -> bool:
    if last_rank_ts is None or last_rank_ts <= 0:
        return False
    return (now_ts - float(last_rank_ts)) <= float(stale_sec)
