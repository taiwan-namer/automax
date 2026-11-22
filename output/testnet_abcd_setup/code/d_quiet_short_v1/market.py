"""Public Binance Futures REST helpers for paper D (read-only)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import aiohttp

from . import config
from .safety import guard_session_request
from .signals import Bar15
from .util import atomic_write_json, now_iso


class GuardedSession:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._sem = asyncio.Semaphore(config.KLINES_API_CONCURRENCY)
        self._last = 0.0

    async def get_json(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        purpose: str = "scan",
        op_budget_sec: Optional[float] = None,
        max_attempts: Optional[int] = None,
        request_timeout_sec: Optional[float] = None,
    ) -> Any:
        if path not in config.ALLOWED_PUBLIC_PATHS:
            raise RuntimeError(f"path not allowed for paper: {path}")
        budget = float(op_budget_sec if op_budget_sec is not None else config.HTTP_OP_BUDGET_SEC)
        attempts = int(max_attempts if max_attempts is not None else config.HTTP_MAX_ATTEMPTS)
        timeout = float(request_timeout_sec if request_timeout_sec is not None else config.HTTP_REQUEST_TIMEOUT_SEC)
        url = config.BINANCE_FUTURES_BASE_URL + path
        start = time.time()
        last_err: Optional[Exception] = None
        for i in range(attempts):
            if time.time() - start > budget:
                break
            wait = max(0.0, 0.05 - (time.time() - self._last))
            if wait:
                await asyncio.sleep(wait)
            try:
                async with self._sem:
                    guard_session_request("GET", url)
                    async with self.session.get(
                        url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        self._last = time.time()
                        if resp.status in (418, 429):
                            await asyncio.sleep(min(2 ** i, 8))
                            continue
                        resp.raise_for_status()
                        return await resp.json()
            except Exception as exc:
                last_err = exc
                await asyncio.sleep(min(0.5 * (2 ** i), 5))
        raise RuntimeError(f"get_json failed path={path} purpose={purpose}: {last_err}")


def _is_excluded(symbol: str, base: str, info: dict[str, Any]) -> tuple[bool, str]:
    if info.get("contractType") != "PERPETUAL":
        return True, "NOT_PERPETUAL"
    if info.get("quoteAsset") != "USDT":
        return True, "NOT_USDT"
    if info.get("status") != "TRADING":
        return True, "NOT_TRADING"
    sym = symbol.upper()
    if sym in config.HARD_EXCLUDE_SYMBOLS:
        return True, "HARD_EXCLUDE_BTC_ETH"
    for suf in config.LEVERAGE_TOKEN_SUFFIXES:
        if sym.endswith(suf):
            return True, "LEVERAGE_TOKEN"
    base_u = base.upper()
    if base_u in config.STABLE_BASES:
        return True, "STABLE_BASE"
    ut = str(info.get("underlyingType") or "").upper()
    if ut in ("INDEX", "PREMARKET", "EQUITY"):
        return True, "UNDERLYING_TYPE"
    subtypes = info.get("underlyingSubType") or []
    items = subtypes if isinstance(subtypes, list) else [subtypes]
    for item in items:
        text = str(item)
        for kw in config.NON_CRYPTO_SUBTYPE_KEYWORDS:
            if kw.lower() in text.lower():
                return True, f"SUBTYPE:{kw}"
    if base_u in config.TRADFI_BASE_BLACKLIST:
        return True, "TRADFI_BASE"
    for prefix in config.TRADFI_SYMBOL_PREFIXES:
        if sym.startswith(prefix) or base_u.startswith(prefix):
            return True, "TRADFI_PREFIX"
    return False, ""


def _lot_filters(sym_info: dict[str, Any]) -> dict[str, Any]:
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


async def refresh_universe(gs: GuardedSession) -> dict[str, Any]:
    info = await gs.get_json("/fapi/v1/exchangeInfo")
    filters: dict[str, dict[str, Any]] = {}
    pure: set[str] = set()
    excluded: list[dict[str, str]] = []
    for s in info.get("symbols") or []:
        sym = str(s.get("symbol") or "")
        base = str(s.get("baseAsset") or "")
        bad, reason = _is_excluded(sym, base, s)
        if bad:
            excluded.append({"symbol": sym, "reason": reason})
            continue
        pure.add(sym)
        filters[sym] = _lot_filters(s)

    tickers = await gs.get_json("/fapi/v1/ticker/24hr")
    rows: list[dict[str, Any]] = []
    for t in tickers:
        sym = t.get("symbol")
        if sym not in pure:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            chg = float(t.get("priceChangePercent") or 0)
            last = float(t.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if qv < config.MIN_QUOTE_VOLUME_USDT or last <= 0:
            continue
        rows.append(
            {
                "symbol": sym,
                "quote_volume_24h": qv,
                "price_change_pct_24h": chg,
                "last_price": last,
            }
        )
    snap = {
        "refreshed_at": now_iso(),
        "eligible_count": len(rows),
        "eligible": rows,
        "filters": filters,
        "excluded_sample_n": len(excluded),
        "hard_exclude": sorted(config.HARD_EXCLUDE_SYMBOLS),
        "large_cap_note": config.LARGE_CAP_NOTE,
        "min_quote_volume_usdt": config.MIN_QUOTE_VOLUME_USDT,
        "quote_volume_definition": config.QUOTE_VOLUME_DEFINITION,
        "max_spread": config.MAX_SPREAD,
        "max_spread_pct_display": config.MAX_SPREAD_PCT_DISPLAY,
        "depth_gate": {
            "confirmed": config.depth_ready(),
            "near_touch_pct": config.DEPTH_NEAR_TOUCH_PCT,
            "min_near_touch_bid_usdt": config.MIN_NEAR_TOUCH_BID_NOTIONAL_USDT,
            "min_near_touch_ask_usdt": config.MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT,
            "walk_notional_usdt": config.DEPTH_WALK_NOTIONAL_USDT,
            "note": config.DEPTH_NOTE,
        },
    }
    paths = config.paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["universe"], {k: snap[k] for k in snap if k != "filters"})
    return snap


async def fetch_book_tickers(gs: GuardedSession, *, manage: bool = False) -> dict[str, dict[str, Any]]:
    if manage:
        data = await gs.get_json(
            "/fapi/v1/ticker/bookTicker",
            purpose="manage_book",
            op_budget_sec=config.MANAGE_BOOK_OP_BUDGET_SEC,
            max_attempts=config.MANAGE_BOOK_MAX_ATTEMPTS,
            request_timeout_sec=config.MANAGE_BOOK_REQUEST_TIMEOUT_SEC,
        )
    else:
        data = await gs.get_json("/fapi/v1/ticker/bookTicker", purpose="scan_book")
    out: dict[str, dict[str, Any]] = {}
    fetched_at = time.time()
    for t in data or []:
        try:
            bid = float(t["bidPrice"])
            ask = float(t["askPrice"])
        except Exception:
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        out[str(t["symbol"])] = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": (ask - bid) / mid if mid > 0 else 999.0,
            "fetched_at": fetched_at,
            "source": "bookTicker",
        }
    return out


def _parse_levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lvl in raw or []:
        try:
            px = float(lvl[0])
            qty = float(lvl[1])
        except Exception:
            continue
        if px > 0 and qty > 0:
            out.append((px, qty))
    return out


def near_touch_notional(
    levels: list[tuple[float, float]],
    *,
    best: float,
    side: str,
    band_pct: float,
) -> float:
    """Sum notional of levels within band of best bid (side=bid) or best ask (side=ask)."""
    if best <= 0 or band_pct <= 0:
        return 0.0
    total = 0.0
    if side == "bid":
        lo = best * (1.0 - band_pct)
        for px, qty in levels:
            if px < lo:
                break  # bids are descending
            if px <= best + 1e-12:
                total += px * qty
    else:
        hi = best * (1.0 + band_pct)
        for px, qty in levels:
            if px > hi:
                break  # asks are ascending
            if px >= best - 1e-12:
                total += px * qty
    return total


def walk_book(
    levels: list[tuple[float, float]],
    *,
    target_notional: Optional[float] = None,
    target_qty: Optional[float] = None,
) -> dict[str, Any]:
    """Walk levels (already ordered) until target notional or qty. Returns VWAP diagnostics."""
    filled_qty = 0.0
    filled_notional = 0.0
    for px, qty in levels:
        if target_qty is not None:
            need = target_qty - filled_qty
            if need <= 0:
                break
            take = min(qty, need)
        else:
            need_n = float(target_notional or 0) - filled_notional
            if need_n <= 0:
                break
            take = min(qty, need_n / px if px > 0 else 0.0)
        if take <= 0:
            continue
        filled_qty += take
        filled_notional += take * px
    vwap = (filled_notional / filled_qty) if filled_qty > 0 else None
    shortfall = False
    if target_qty is not None:
        shortfall = filled_qty + 1e-12 < float(target_qty)
    elif target_notional is not None:
        shortfall = filled_notional + 1e-6 < float(target_notional)
    return {
        "filled_qty": filled_qty,
        "filled_notional": filled_notional,
        "vwap": vwap,
        "shortfall": shortfall,
        "levels_used_hint": len(levels),
    }


def analyze_order_book(raw: dict[str, Any], *, target_walk_notional: float) -> dict[str, Any]:
    """Dual near-touch depth + walk sims for short entry (sell bids) and cover (buy asks)."""
    bids = _parse_levels(raw.get("bids"))
    asks = _parse_levels(raw.get("asks"))
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    band = float(config.DEPTH_NEAR_TOUCH_PCT)
    bid_near = near_touch_notional(bids, best=best_bid, side="bid", band_pct=band)
    ask_near = near_touch_notional(asks, best=best_ask, side="ask", band_pct=band)
    # short entry consumes bids; cover exit consumes asks
    walk_entry = walk_book(bids, target_notional=target_walk_notional)
    walk_exit = walk_book(asks, target_notional=target_walk_notional)
    entry_slip = None
    exit_slip = None
    if walk_entry["vwap"] and best_bid > 0:
        # selling into bids: adverse if VWAP < best bid
        entry_slip = (best_bid - float(walk_entry["vwap"])) / best_bid
    if walk_exit["vwap"] and best_ask > 0:
        # buying asks: adverse if VWAP > best ask
        exit_slip = (float(walk_exit["vwap"]) - best_ask) / best_ask
    entry_ok = (
        bid_near >= float(config.MIN_NEAR_TOUCH_BID_NOTIONAL_USDT)
        and ask_near >= float(config.MIN_NEAR_TOUCH_ASK_NOTIONAL_USDT)
    )
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "near_touch_pct": band,
        "near_touch_bid_notional": bid_near,
        "near_touch_ask_notional": ask_near,
        "entry_depth_ok": entry_ok,
        "walk_entry_sell_bids": walk_entry,
        "walk_exit_buy_asks": walk_exit,
        "entry_slip_vs_best_bid": entry_slip,
        "exit_slip_vs_best_ask": exit_slip,
        "note": config.DEPTH_NOTE,
    }


async def fetch_depth_analysis(gs: GuardedSession, symbol: str) -> Optional[dict[str, Any]]:
    try:
        data = await gs.get_json(
            "/fapi/v1/depth",
            params={"symbol": symbol, "limit": config.DEPTH_API_LIMIT},
            purpose="depth",
            op_budget_sec=8.0,
            max_attempts=2,
            request_timeout_sec=5.0,
        )
    except Exception:
        return None
    return analyze_order_book(data or {}, target_walk_notional=float(config.DEPTH_WALK_NOTIONAL_USDT))


async def fetch_depth_bid_notional(gs: GuardedSession, symbol: str) -> Optional[float]:
    """Compat wrapper: returns near-touch BID notional only (not sufficient alone for gate)."""
    diag = await fetch_depth_analysis(gs, symbol)
    if not diag:
        return None
    return float(diag["near_touch_bid_notional"])


async def fetch_completed_15m_bars(
    gs: GuardedSession, symbol: str, limit: int = config.KLINES_FETCH_LIMIT
) -> list[Bar15]:
    raw = await gs.get_json(
        "/fapi/v1/klines",
        params={"symbol": symbol, "interval": config.KLINE_INTERVAL, "limit": limit},
        purpose="scan_kline_15m",
        op_budget_sec=min(config.HTTP_OP_BUDGET_SEC, config.SCAN_KLINE_WAIT_SEC),
        max_attempts=3,
        request_timeout_sec=min(config.HTTP_REQUEST_TIMEOUT_SEC, 6.0),
    )
    now_ms = int(time.time() * 1000)
    bars: list[Bar15] = []
    for x in raw or []:
        ot = int(x[0])
        ct = int(x[6])
        if ct >= now_ms:
            continue  # incomplete bar forbidden for signals
        bars.append(
            Bar15(
                open_time=ot,
                open=float(x[1]),
                high=float(x[2]),
                low=float(x[3]),
                close=float(x[4]),
                close_time=ct,
            )
        )
    return bars


async def fetch_last_closed_1m_stats(gs: GuardedSession, symbol: str) -> dict[str, Any]:
    """Last fully closed 1m bar stats for observation logging (decision-time known)."""
    try:
        raw = await gs.get_json(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 3},
            purpose="observe_1m",
            op_budget_sec=6.0,
            max_attempts=2,
            request_timeout_sec=4.0,
        )
    except Exception as exc:
        return {"status": "MISSING", "error": str(exc)}
    now_ms = int(time.time() * 1000)
    closed = []
    for x in raw or []:
        ot = int(x[0])
        ct = int(x[6])
        if ct >= now_ms:
            continue
        o, h, l, c = float(x[1]), float(x[2]), float(x[3]), float(x[4])
        closed.append({"open_time": ot, "close_time": ct, "o": o, "h": h, "l": l, "c": c})
    if not closed:
        return {"status": "MISSING", "error": "no_closed_1m"}
    b = closed[-1]
    if b["o"] <= 0:
        return {"status": "MISSING", "error": "bad_open"}
    return {
        "status": "OK",
        "open_time": b["open_time"],
        "close_time": b["close_time"],
        "ret_pct": (b["c"] - b["o"]) / b["o"] * 100.0,
        "amp_pct": (b["h"] - b["l"]) / b["o"] * 100.0,
    }
