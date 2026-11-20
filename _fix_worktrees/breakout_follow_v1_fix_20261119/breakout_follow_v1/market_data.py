"""Public market data only (exchangeInfo / ticker / klines / bookTicker / premiumIndex)."""

from __future__ import annotations

import asyncio
import hashlib
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

import aiohttp
import numpy as np
import pandas as pd

from . import config
from .safety import guard_session_request
from .util import atomic_write_json, now_iso, report_error

# K 線資料層常數（非交易參數；刻意不放 config 以免改變 config_hash）
KLINE_INTERVAL = "1m"
KLINE_FINAL_GRACE_MS = 1_000  # 資料來源時間須超過 close_time 至少此值才視為已收盤
# 乾淨快取（v2）；舊 kline_vault（keep-first 污染）保留封存、不再讀寫
CLEAN_VAULT_DIR = Path("data") / config.STRATEGY_ID / "kline_vault_v2"
VAULT_MAX_ROWS = 2000
KLINE_COLUMNS = [
    "symbol", "interval", "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "is_final", "final_basis", "source_time_ms",
    "observed_at_ms", "origin",
]
_PRICE_COLS = ("open", "high", "low", "close", "volume", "quote_volume")


class GuardedSession:
    """aiohttp session wrapper that rejects non-GET and forbidden paths."""

    def __init__(self, session: aiohttp.ClientSession):
        self._s = session

    async def get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        data, _meta = await self.get_json_with_meta(path, params)
        return data

    async def get_json_with_meta(self, path: str, params: Optional[dict[str, Any]] = None) -> tuple[Any, dict[str, Any]]:
        """回傳 (json, meta)；meta.server_date_ms 取自回應 Date header（資料來源時間，秒精度）。"""
        url = f"{config.BINANCE_FUTURES_BASE_URL}{path}"
        guard_session_request("GET", url)
        request_ms = int(time.time() * 1000)
        async with self._s.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            date_hdr = resp.headers.get("Date")
        response_ms = int(time.time() * 1000)
        server_ms = None
        if date_hdr:
            try:
                server_ms = int(parsedate_to_datetime(date_hdr).timestamp() * 1000)
            except (TypeError, ValueError) as exc:
                report_error("SERVER_DATE_PARSE_FAIL", f"{path} {date_hdr!r}: {exc}")
        return data, {"request_ms": request_ms, "response_ms": response_ms, "server_date_ms": server_ms}


def _normalize_base(base_asset: str) -> str:
    base = base_asset.upper()
    for prefix in ("1000000", "100000", "10000", "1000"):
        if base.startswith(prefix) and len(base) > len(prefix):
            rest = base[len(prefix) :]
            if rest and rest[0].isalpha():
                return rest
    return base


def _is_non_crypto(symbol: str, base_asset: str, info: dict[str, Any]) -> bool:
    if info.get("contractType") != "PERPETUAL":
        return True
    if info.get("quoteAsset") != "USDT":
        return True
    if info.get("status") != "TRADING":
        return True
    # underlyingType: prefer COIN; if missing, still allow if not TradFi
    ut = str(info.get("underlyingType") or "")
    if ut and ut.upper() not in ("COIN", "CRYPTOCURRENCY", ""):
        # some listings use empty; TradFi often INDEX etc.
        if ut.upper() in ("INDEX", "PREMARKET", "EQUITY"):
            return True
    subtypes = info.get("underlyingSubType") or []
    items = subtypes if isinstance(subtypes, list) else [subtypes]
    for item in items:
        text = str(item)
        for kw in config.NON_CRYPTO_SUBTYPE_KEYWORDS:
            if kw.lower() in text.lower():
                return True
    base = base_asset.upper()
    norm = _normalize_base(base)
    if norm in config.TRADFI_BASE_BLACKLIST or base in config.TRADFI_BASE_BLACKLIST:
        return True
    sym = symbol.upper()
    for prefix in config.TRADFI_SYMBOL_PREFIXES:
        if sym.startswith(prefix) or base.startswith(prefix):
            return True
    return False


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


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    precision = max(0, int(round(-np.log10(step)))) if step < 1 else 0
    n = np.floor(qty / step + 1e-12) * step
    return float(round(n, precision + 2))


_KLINE_SEM: asyncio.Semaphore | None = None
_KLINE_LOCK: asyncio.Lock | None = None
_KLINE_LAST = 0.0


async def _rate_wait() -> None:
    global _KLINE_LAST
    if _KLINE_LOCK is None:
        return
    async with _KLINE_LOCK:
        now = time.time()
        wait = config.KLINES_MIN_INTERVAL_SEC - (now - _KLINE_LAST)
        if wait > 0:
            await asyncio.sleep(wait)
        _KLINE_LAST = time.time()


def _vault_path(symbol: str) -> Path:
    return CLEAN_VAULT_DIR / f"{symbol}_{KLINE_INTERVAL}.csv"


def _normalize_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """補齊欄位；缺 is_final 的舊列一律視為未確認收盤。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=KLINE_COLUMNS)
    out = df.copy()
    defaults = {
        "symbol": symbol, "interval": KLINE_INTERVAL, "is_final": False, "final_basis": "UNKNOWN_LEGACY",
        "source_time_ms": -1, "observed_at_ms": 0, "origin": "UNKNOWN_LEGACY",
    }
    for col, val in defaults.items():
        if col not in out.columns:
            out[col] = val
    out["is_final"] = out["is_final"].map(lambda v: v is True or str(v).strip().lower() == "true")
    out["open_time"] = out["open_time"].astype("int64")
    out["close_time"] = out["close_time"].astype("int64")
    out["observed_at_ms"] = out["observed_at_ms"].fillna(0).astype("int64")
    out["source_time_ms"] = out["source_time_ms"].fillna(-1).astype("int64")
    return out[[c for c in KLINE_COLUMNS if c in out.columns]]


def load_vault(symbol: str) -> pd.DataFrame:
    p = _vault_path(symbol)
    if not p.exists():
        return pd.DataFrame(columns=KLINE_COLUMNS)
    try:
        return _normalize_frame(pd.read_csv(p), symbol)
    except Exception as exc:
        report_error("KLINE_VAULT_READ_FAIL", f"{p}: {exc}", symbol=symbol)
        return pd.DataFrame(columns=KLINE_COLUMNS)


def save_vault(symbol: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    out = df.sort_values("open_time").tail(VAULT_MAX_ROWS)
    p = _vault_path(symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(p, index=False)


def parse_kline_rows(symbol: str, raw: list, meta: dict[str, Any], *, origin: str) -> pd.DataFrame:
    """
    依「資料來源時間」判斷每根是否已收盤：server Date ≥ close_time + 1 + grace 才標 is_final。
    沒有來源時間時一律不標收盤（不以本機時間推定）。
    """
    src = meta.get("server_date_ms")
    observed = int(meta.get("response_ms") or meta.get("request_ms") or 0)
    rows = []
    for x in raw:
        close_time = int(x[6])
        if src is None:
            final, basis = False, "NO_SOURCE_TIME"
        else:
            final = int(src) >= close_time + 1 + KLINE_FINAL_GRACE_MS
            basis = "SERVER_DATE"
        rows.append({
            "symbol": symbol, "interval": KLINE_INTERVAL, "open_time": int(x[0]),
            "open": float(x[1]), "high": float(x[2]), "low": float(x[3]), "close": float(x[4]),
            "volume": float(x[5]), "close_time": close_time, "quote_volume": float(x[7]),
            "is_final": bool(final), "final_basis": basis, "source_time_ms": int(src) if src is not None else -1,
            "observed_at_ms": observed, "origin": origin,
        })
    return pd.DataFrame(rows, columns=KLINE_COLUMNS)


def merge_klines(cached: pd.DataFrame, fresh: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    以 (symbol, interval, open_time) 合併：
    - 已收盤版本不被未收盤或較舊資料覆蓋（兩個收盤版本數值不同時保留先確認者並計 conflict）。
    - 未收盤版本可被收盤版本或觀測時間較新的未收盤版本取代；較舊的回應忽略。
    回傳 (merged, stats)；stats["changed_open_times"] 為本次真正寫入新資訊的 open_time。
    """
    stats: dict[str, Any] = {
        "inserted": 0, "upgraded_to_final": 0, "refreshed_nonfinal": 0, "kept_final": 0,
        "stale_ignored": 0, "final_conflicts": 0, "changed_open_times": [], "conflict_open_times": [],
    }
    cur: dict[tuple, dict[str, Any]] = {}
    if cached is not None and not cached.empty:
        for r in cached.to_dict("records"):
            cur[(r["symbol"], r["interval"], int(r["open_time"]))] = r
    if fresh is not None and not fresh.empty:
        for r in fresh.to_dict("records"):
            key = (r["symbol"], r["interval"], int(r["open_time"]))
            old = cur.get(key)
            if old is None:
                cur[key] = r
                stats["inserted"] += 1
                stats["changed_open_times"].append(key[2])
            elif bool(old["is_final"]):
                if bool(r["is_final"]) and any(abs(float(old[c]) - float(r[c])) > 1e-12 for c in _PRICE_COLS):
                    stats["final_conflicts"] += 1
                    stats["conflict_open_times"].append(key[2])
                else:
                    stats["kept_final"] += 1
            elif bool(r["is_final"]):
                cur[key] = r
                stats["upgraded_to_final"] += 1
                stats["changed_open_times"].append(key[2])
            elif int(r["observed_at_ms"]) > int(old["observed_at_ms"]):
                cur[key] = r
                stats["refreshed_nonfinal"] += 1
                stats["changed_open_times"].append(key[2])
            else:
                stats["stale_ignored"] += 1
    merged = pd.DataFrame(list(cur.values()), columns=KLINE_COLUMNS)
    if not merged.empty:
        merged = merged.sort_values("open_time").reset_index(drop=True)
    return merged, stats


async def refresh_universe(gs: GuardedSession) -> dict[str, Any]:
    """合格清單 + 漲跌前10。新策略不過濾主流大幣。"""
    info = await gs.get_json("/fapi/v1/exchangeInfo")
    filters: dict[str, dict[str, Any]] = {}
    pure: set[str] = set()
    for s in info.get("symbols") or []:
        sym = str(s.get("symbol") or "")
        base = str(s.get("baseAsset") or "")
        if _is_non_crypto(sym, base, s):
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

    by_chg = sorted(rows, key=lambda x: x["price_change_pct_24h"], reverse=True)
    gainers = by_chg[: config.TOP_N]
    losers = sorted(rows, key=lambda x: x["price_change_pct_24h"])[: config.TOP_N]
    g_set = {g["symbol"] for g in gainers}
    losers = [x for x in losers if x["symbol"] not in g_set][: config.TOP_N]

    snap = {
        "refreshed_at": now_iso(),
        "eligible_count": len(rows),
        "eligible": rows,
        "top_gainers": [{**g, "rank": i} for i, g in enumerate(gainers, 1)],
        "top_losers": [{**lo, "rank": i} for i, lo in enumerate(losers, 1)],
        "filters": filters,
    }
    atomic_write_json(config.UNIVERSE_PATH, {
        "refreshed_at": snap["refreshed_at"],
        "eligible_count": snap["eligible_count"],
        "top_gainers": snap["top_gainers"],
        "top_losers": snap["top_losers"],
    })
    return snap


async def fetch_book_tickers(gs: GuardedSession) -> dict[str, dict[str, Any]]:
    data = await gs.get_json("/fapi/v1/ticker/bookTicker")
    fetched_at = time.time()
    out: dict[str, dict[str, Any]] = {}
    for t in data:
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


async def fetch_klines_incremental(
    gs: GuardedSession,
    symbol: str,
    limit: int = config.KLINES_FETCH_LIMIT,
    *,
    origin: str = "LIVE_SCAN",
) -> tuple[pd.DataFrame, Optional[str]]:
    """增量合併乾淨 vault；失敗回傳 (舊資料或空, error)。原始觀測另寫入 run 的 append-only 紀錄。"""
    global _KLINE_SEM, _KLINE_LOCK
    if _KLINE_SEM is None:
        _KLINE_SEM = asyncio.Semaphore(config.KLINES_API_CONCURRENCY)
    if _KLINE_LOCK is None:
        _KLINE_LOCK = asyncio.Lock()

    from .run_store import get_store

    store = get_store()
    cached = load_vault(symbol)
    try:
        async with _KLINE_SEM:
            await _rate_wait()
            raw, meta = await gs.get_json_with_meta(
                "/fapi/v1/klines",
                params={"symbol": symbol, "interval": KLINE_INTERVAL, "limit": limit},
            )
        fresh = parse_kline_rows(symbol, raw, meta, origin=origin)
        merged, mstats = merge_klines(cached, fresh)
        save_vault(symbol, merged)
    except Exception as exc:
        report_error("KLINE_FETCH_FAIL", f"{type(exc).__name__}: {exc}", symbol=symbol)
        return cached, f"{type(exc).__name__}: {exc}"

    if meta.get("server_date_ms") is None:
        report_error("KLINE_NO_SOURCE_TIME", "response had no Date header; bars kept non-final", symbol=symbol)
    if mstats["final_conflicts"]:
        report_error("KLINE_FINAL_CONFLICT", f"open_times={mstats['conflict_open_times']}", symbol=symbol)
    if store is not None:
        try:
            changed = set(mstats["changed_open_times"])
            obs = fresh[fresh["open_time"].isin(changed)]
            store.log_kline_observations(symbol, obs, meta=meta, stats={k: v for k, v in mstats.items() if k != "changed_open_times"})
            store.archive_klines(symbol, merged, note=f"incremental_fetch:{origin}")
        except Exception as exc:
            report_error("KLINE_ARCHIVE_FAIL", f"{type(exc).__name__}: {exc}", symbol=symbol)
    return merged, None


async def backfill_warmup_klines(
    gs: GuardedSession,
    symbols: list[str],
    *,
    min_bars: int = 100,
) -> dict[str, Any]:
    """
    觀測用：為本 run 歸檔補足至少 min_bars 根 1m（不改交易門檻）。
    對每個 symbol 額外拉 limit=max(min_bars+20, 150)；列標 origin=WARMUP_BACKFILL。
    """
    from .run_store import get_store

    store = get_store()
    need = max(min_bars + 20, 150)
    summary: dict[str, Any] = {"requested": len(symbols), "ok": 0, "fail": 0, "short_after": []}
    for sym in symbols:
        df, err = await fetch_klines_incremental(gs, sym, limit=need, origin="WARMUP_BACKFILL")
        if err or df is None or df.empty:
            summary["fail"] += 1
            report_error("WARMUP_BACKFILL_FAIL", str(err), symbol=sym)
            continue
        n_final = int(df["is_final"].sum())
        if n_final < min_bars:
            summary["short_after"].append({"symbol": sym, "final_bars": n_final})
            report_error("KLINE_WARMUP_SHORT", f"after_backfill final_bars={n_final}<{min_bars}", symbol=sym)
        else:
            summary["ok"] += 1
    return summary


def closed_klines(df: pd.DataFrame, now_ms: Optional[int] = None) -> pd.DataFrame:
    """只回傳已確認收盤（is_final）的列；沒有 is_final 欄位的資料不視為合格。"""
    if df is None or df.empty or "is_final" not in df.columns:
        return pd.DataFrame(columns=list(df.columns) if df is not None else KLINE_COLUMNS)
    now_ms = now_ms or int(time.time() * 1000)
    fin = df["is_final"].map(lambda v: v is True or str(v).strip().lower() == "true")
    mask = fin & (df["close_time"].astype("int64") < now_ms)
    return df.loc[mask].sort_values("open_time").reset_index(drop=True)


def contiguous_gaps(df: pd.DataFrame) -> list[int]:
    """回傳缺漏的 open_time（相鄰列間距不是 60 秒者）。"""
    if df is None or len(df) < 2:
        return []
    ot = df["open_time"].astype("int64").to_numpy()
    missing: list[int] = []
    for a, b in zip(ot[:-1], ot[1:]):
        t = int(a) + 60_000
        while t < int(b):
            missing.append(t)
            t += 60_000
    return missing


def window_digest(df: pd.DataFrame) -> str:
    cols = [c for c in ("open_time", "open", "high", "low", "close", "volume", "close_time", "is_final", "observed_at_ms") if c in df.columns]
    return hashlib.sha256(df[cols].to_csv(index=False).encode()).hexdigest()


def latest_final_atr(df: pd.DataFrame, now_ms: Optional[int] = None) -> tuple[Optional[float], Optional[str]]:
    """持倉管理用 ATR：只用已收盤且連續的最後 ATR_PERIOD+5 根；不合格時回傳 (None, 原因)。"""
    cl = closed_klines(df, now_ms=now_ms)
    n = config.ATR_PERIOD + 5
    if len(cl) < config.ATR_PERIOD + 1:
        return None, f"INSUFFICIENT_FINAL_BARS:{len(cl)}"
    tail = cl.tail(n)
    gaps = contiguous_gaps(tail)
    if gaps:
        return None, f"KLINE_GAP:{len(gaps)}"
    return wilder_atr(tail, config.ATR_PERIOD), None


def wilder_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Wilder ATR on fully provided closed bars (no future bar)."""
    if df is None or len(df) < period + 1:
        return None
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    tr = np.zeros(len(df))
    tr[0] = h[0] - l[0]
    for i in range(1, len(df)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = float(np.mean(tr[1 : period + 1]))
    for i in range(period + 1, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return float(atr)


async def fetch_premium_index(gs: GuardedSession, symbol: str) -> dict[str, Any]:
    try:
        data = await gs.get_json("/fapi/v1/premiumIndex", params={"symbol": symbol})
        return {
            "lastFundingRate": float(data.get("lastFundingRate") or 0),
            "nextFundingTime": int(data.get("nextFundingTime") or 0),
            "markPrice": float(data.get("markPrice") or 0),
            "ok": True,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "funding_data_gap": True}
