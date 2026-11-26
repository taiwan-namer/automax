"""Public Binance Futures REST helpers for H paper (read-only)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import aiohttp

from . import config
from .universe import is_excluded, lot_filters, rank_by_24h_change
from .util import append_jsonl, atomic_write_json, now_iso, now_ts


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
        from .safety import assert_public_url

        if path not in config.ALLOWED_PUBLIC_PATHS:
            raise RuntimeError(f"path not allowed for paper: {path}")
        url = config.BINANCE_FUTURES_BASE_URL + path
        assert_public_url(url)
        budget = float(op_budget_sec if op_budget_sec is not None else config.HTTP_OP_BUDGET_SEC)
        attempts = int(max_attempts if max_attempts is not None else config.HTTP_MAX_ATTEMPTS)
        timeout = float(request_timeout_sec if request_timeout_sec is not None else config.HTTP_REQUEST_TIMEOUT_SEC)
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


async def refresh_universe(gs: GuardedSession) -> dict[str, Any]:
    info = await gs.get_json("/fapi/v1/exchangeInfo")
    filters: dict[str, dict[str, Any]] = {}
    pure: set[str] = set()
    excluded: list[dict[str, str]] = []
    tradable = 0
    for s in info.get("symbols") or []:
        sym = str(s.get("symbol") or "")
        base = str(s.get("baseAsset") or "")
        if not sym:
            continue
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            tradable += 1
        bad, reason = is_excluded(sym, base, s)
        if bad:
            excluded.append({"symbol": sym, "base": base, "reason": reason})
            continue
        pure.add(sym)
        filters[sym] = lot_filters(s)

    tickers = await gs.get_json("/fapi/v1/ticker/24hr")
    rows: list[dict[str, Any]] = []
    for t in tickers:
        sym = t.get("symbol")
        if sym not in pure:
            continue
        try:
            chg = float(t.get("priceChangePercent") or 0)
            last = float(t.get("lastPrice") or 0)
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0:
            continue
        rows.append(
            {
                "symbol": sym,
                "quote_volume_24h": qv,
                "price_change_pct_24h": chg,
                "last_price": last,
            }
        )

    ranked = rank_by_24h_change(rows)
    snap = {
        "ok": True,
        "refreshed_at": now_iso(),
        "refreshed_ts": now_ts(),
        "tradable_usdt_perp": tradable,
        "excluded_n": len(excluded),
        "eligible_n": len(rows),
        "ranked": ranked,
        "top5": [r for r in ranked if int(r["rank"]) <= config.TOP_ENTER],
        "top15": [r for r in ranked if int(r["rank"]) <= config.TOP_KEEP],
        "filters": filters,
        "excluded_sample": excluded[:120],
        "strategy_version": config.STRATEGY_VERSION,
        "config_hash": config.config_hash(),
        "note": "snapshot_at_refresh_no_future_rank",
    }
    paths = config.shared_paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["universe"], {k: snap[k] for k in snap if k != "filters"})
    append_jsonl(
        paths["rank_snapshots"],
        {
            "event": "RANK_SNAPSHOT",
            "at": snap["refreshed_at"],
            "ts": snap["refreshed_ts"],
            "top5": snap["top5"],
            "top15": [{"rank": r["rank"], "symbol": r["symbol"], "price_change_pct_24h": r["price_change_pct_24h"]} for r in snap["top15"]],
            "excluded_n": snap["excluded_n"],
            "tradable_usdt_perp": tradable,
            "strategy_version": config.STRATEGY_VERSION,
            "config_hash": config.config_hash(),
        },
    )
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
        out[str(t["symbol"])] = {"bid": bid, "ask": ask, "fetched_at": fetched_at}
    return out
