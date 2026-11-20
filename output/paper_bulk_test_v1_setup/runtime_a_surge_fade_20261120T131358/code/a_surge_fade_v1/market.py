"""Public Binance Futures REST helpers for paper A (read-only)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import aiohttp

from . import config
from .signals import Bar
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
            wait = max(0.0, config.KLINES_MIN_INTERVAL_SEC - (time.time() - self._last))
            if wait:
                await asyncio.sleep(wait)
            try:
                async with self._sem:
                    async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
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
        "large_cap_note": "only BTC/ETH hard-excluded among large caps; no full large-cap denylist claimed",
        "min_quote_volume_usdt": config.MIN_QUOTE_VOLUME_USDT,
        "max_spread": config.MAX_SPREAD,
    }
    from . import config as cfg

    paths = cfg.shared_paths()
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
        }
    return out


async def fetch_completed_1m_bars(gs: GuardedSession, symbol: str, limit: int = config.KLINES_FETCH_LIMIT) -> list[Bar]:
    # Need >=1441 completed 1m bars for true 24h close-to-close; keep limit but tight budget.
    raw = await gs.get_json(
        "/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1m", "limit": limit},
        purpose="scan_kline",
        op_budget_sec=min(config.HTTP_OP_BUDGET_SEC, getattr(config, "SCAN_KLINE_WAIT_SEC", 8.0)),
        max_attempts=3,
        request_timeout_sec=min(config.HTTP_REQUEST_TIMEOUT_SEC, 6.0),
    )
    now_ms = int(time.time() * 1000)
    bars: list[Bar] = []
    for x in raw or []:
        ot = int(x[0])
        ct = int(x[6])
        # only completed
        if ct >= now_ms:
            continue
        bars.append(
            Bar(
                open_time=ot,
                open=float(x[1]),
                high=float(x[2]),
                low=float(x[3]),
                close=float(x[4]),
                close_time=ct,
            )
        )
    return bars
