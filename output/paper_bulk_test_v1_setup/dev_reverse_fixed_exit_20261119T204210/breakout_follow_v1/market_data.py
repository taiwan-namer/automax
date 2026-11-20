"""Public market data only (exchangeInfo / ticker / klines / bookTicker / premiumIndex)."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional

import aiohttp
import numpy as np
import pandas as pd

from . import config
from .safety import guard_session_request
from .util import atomic_write_json, now_iso


class PublicHttpError(RuntimeError):
    """公開行情請求失敗（含預算耗盡／封禁）；不含機密。"""

    def __init__(self, message: str, *, endpoint: str, status: Optional[int] = None, attempts: int = 0):
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.attempts = attempts


class EndpointBanned(PublicHttpError):
    """418 或明確封禁：停止該端點請求，不密集重試。"""


# 進程內共用冷卻／封禁（並行 worker 共用；非跨程序全域限流）
_HTTP_LOCK: asyncio.Lock | None = None
_ENDPOINT_COOLDOWN_UNTIL: dict[str, float] = {}
_ENDPOINT_BANNED: dict[str, dict[str, Any]] = {}
_HTTP_EVENT_LOG: list[dict[str, Any]] = []
_HTTP_EVENT_CAP = 200


def _http_lock() -> asyncio.Lock:
    global _HTTP_LOCK
    if _HTTP_LOCK is None:
        _HTTP_LOCK = asyncio.Lock()
    return _HTTP_LOCK


def reset_http_reliability_state_for_tests() -> None:
    """測試專用：清空冷卻／封禁／事件。"""
    _ENDPOINT_COOLDOWN_UNTIL.clear()
    _ENDPOINT_BANNED.clear()
    _HTTP_EVENT_LOG.clear()


def get_http_event_log() -> list[dict[str, Any]]:
    return list(_HTTP_EVENT_LOG)


def _log_http_event(row: dict[str, Any]) -> None:
    row = {"ts": now_iso(), **row}
    _HTTP_EVENT_LOG.append(row)
    if len(_HTTP_EVENT_LOG) > _HTTP_EVENT_CAP:
        del _HTTP_EVENT_LOG[: len(_HTTP_EVENT_LOG) - _HTTP_EVENT_CAP]
    try:
        from .run_store import get_store

        store = get_store()
        if store is not None:
            code = str(row.get("result") or "HTTP_EVENT")
            detail = (
                f"endpoint={row.get('endpoint')} status={row.get('status')} "
                f"attempt={row.get('attempt')}/{row.get('max_attempts')} "
                f"backoff_sec={row.get('backoff_sec')} final={row.get('final')}"
            )
            store.note_quality(code, detail=detail[:500], replay_precise=False)
    except Exception:
        pass


def parse_retry_after_seconds(headers: Any, *, now: Optional[float] = None) -> Optional[float]:
    """解析 Retry-After（秒數或 HTTP-date）；無效則 None。"""
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    except Exception:
        raw = None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    # HTTP-date
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        now = time.time() if now is None else now
        return max(0.0, dt.timestamp() - now)
    except Exception:
        return None


def compute_backoff_seconds(attempt_index: int, *, retry_after: Optional[float] = None) -> float:
    """
    attempt_index 從 0 起。有有效 Retry-After 則採用（可加小幅 jitter）；
    否則有界指數退避 + jitter。不得低於 Retry-After。
    """
    if retry_after is not None and retry_after >= 0:
        base = float(retry_after)
        jitter = base * config.HTTP_BACKOFF_JITTER_FRAC * random.random()
        return min(config.HTTP_BACKOFF_MAX_SEC * 4, base + jitter)  # Retry-After 可略超 backoff max，但仍有硬帽
    exp = config.HTTP_BACKOFF_BASE_SEC * (2 ** max(0, attempt_index))
    exp = min(exp, config.HTTP_BACKOFF_MAX_SEC)
    jitter = exp * config.HTTP_BACKOFF_JITTER_FRAC * random.random()
    return exp + jitter


async def _shared_cooldown_remaining(endpoint: str) -> float:
    async with _http_lock():
        until = float(_ENDPOINT_COOLDOWN_UNTIL.get(endpoint) or 0.0)
        return max(0.0, until - time.time())


async def _set_shared_cooldown(endpoint: str, seconds: float) -> float:
    """延長（不縮短）進程內該端點冷卻；回傳最終 until。"""
    async with _http_lock():
        now = time.time()
        until = max(float(_ENDPOINT_COOLDOWN_UNTIL.get(endpoint) or 0.0), now + max(0.0, seconds))
        _ENDPOINT_COOLDOWN_UNTIL[endpoint] = until
        return until


async def _ban_endpoint(endpoint: str, *, status: int, detail: str) -> None:
    async with _http_lock():
        _ENDPOINT_BANNED[endpoint] = {
            "status": status,
            "detail": detail,
            "banned_at": now_iso(),
        }
    print(f"[ALERT] endpoint banned status={status} path={endpoint} detail={detail} — stop dense retries")
    _log_http_event(
        {
            "endpoint": endpoint,
            "status": status,
            "result": "ENDPOINT_BANNED",
            "final": True,
            "detail": detail,
        }
    )


def _is_banned(endpoint: str) -> Optional[dict[str, Any]]:
    return _ENDPOINT_BANNED.get(endpoint)


class GuardedSession:
    """aiohttp session wrapper：僅公開 GET；429 退避／418 封禁；進程內共用冷卻。"""

    def __init__(self, session: aiohttp.ClientSession):
        self._s = session

    async def get_json(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        op_budget_sec: Optional[float] = None,
        max_attempts: Optional[int] = None,
        request_timeout_sec: Optional[float] = None,
        purpose: str = "default",
    ) -> Any:
        """公開 GET。可覆寫單次預算／attempts／timeout（管理 book 短預算）；429／418 共用冷卻不變。"""
        url = f"{config.BINANCE_FUTURES_BASE_URL}{path}"
        guard_session_request("GET", url)
        endpoint = path.split("?")[0]
        banned = _is_banned(endpoint)
        if banned:
            raise EndpointBanned(
                f"endpoint banned: {endpoint} status={banned.get('status')}",
                endpoint=endpoint,
                status=int(banned.get("status") or 418),
                attempts=0,
            )

        op_start = time.time()
        budget = float(config.HTTP_OP_BUDGET_SEC if op_budget_sec is None else op_budget_sec)
        max_attempts_i = int(config.HTTP_MAX_ATTEMPTS if max_attempts is None else max_attempts)
        req_timeout = float(
            config.HTTP_REQUEST_TIMEOUT_SEC if request_timeout_sec is None else request_timeout_sec
        )
        last_status: Optional[int] = None
        last_err: Optional[str] = None

        for attempt in range(max_attempts_i):
            elapsed = time.time() - op_start
            remain = budget - elapsed
            if remain <= 0:
                _log_http_event(
                    {
                        "endpoint": endpoint,
                        "status": last_status,
                        "attempt": attempt,
                        "max_attempts": max_attempts_i,
                        "result": "OP_BUDGET_EXCEEDED",
                        "final": True,
                        "backoff_sec": None,
                        "purpose": purpose,
                    }
                )
                raise PublicHttpError(
                    f"op budget exceeded for {endpoint}",
                    endpoint=endpoint,
                    status=last_status,
                    attempts=attempt,
                )

            # 共用冷卻：必須等到冷卻結束；若冷卻長於剩餘預算則結束且不提前重送
            cool = await _shared_cooldown_remaining(endpoint)
            if cool > 0:
                if cool > remain:
                    _log_http_event(
                        {
                            "endpoint": endpoint,
                            "status": last_status,
                            "attempt": attempt,
                            "max_attempts": max_attempts_i,
                            "result": "COOLDOWN_EXCEEDS_BUDGET",
                            "final": True,
                            "backoff_sec": cool,
                            "purpose": purpose,
                        }
                    )
                    raise PublicHttpError(
                        f"cooldown {cool:.3f}s exceeds remaining budget for {endpoint}",
                        endpoint=endpoint,
                        status=last_status,
                        attempts=attempt,
                    )
                await asyncio.sleep(cool)

            timeout = aiohttp.ClientTimeout(total=min(req_timeout, max(0.1, remain)))
            try:
                async with self._s.get(url, params=params, timeout=timeout) as resp:
                    status = int(resp.status)
                    last_status = status
                    if status == 418:
                        await _ban_endpoint(endpoint, status=418, detail="teapot/ban")
                        raise EndpointBanned(
                            f"HTTP 418 banned {endpoint}",
                            endpoint=endpoint,
                            status=418,
                            attempts=attempt + 1,
                        )
                    if status == 429:
                        ra = parse_retry_after_seconds(resp.headers)
                        backoff = compute_backoff_seconds(attempt, retry_after=ra)
                        await _set_shared_cooldown(endpoint, backoff)
                        _log_http_event(
                            {
                                "endpoint": endpoint,
                                "status": 429,
                                "attempt": attempt + 1,
                                "max_attempts_i": max_attempts_i,
                                "result": "HTTP_429_BACKOFF",
                                "final": False,
                                "backoff_sec": backoff,
                                "retry_after": ra,
                            }
                        )
                        # 若退避超過剩餘預算：保留冷卻、不提前重送、結束本操作
                        remain_after = budget - (time.time() - op_start)
                        if backoff > remain_after or attempt + 1 >= max_attempts_i:
                            _log_http_event(
                                {
                                    "endpoint": endpoint,
                                    "status": 429,
                                    "attempt": attempt + 1,
                                    "max_attempts_i": max_attempts_i,
                                    "result": "HTTP_429_GAVE_UP",
                                    "final": True,
                                    "backoff_sec": backoff,
                                    "retry_after": ra,
                                }
                            )
                            raise PublicHttpError(
                                f"HTTP 429 exhausted/budget for {endpoint}",
                                endpoint=endpoint,
                                status=429,
                                attempts=attempt + 1,
                            )
                        await asyncio.sleep(backoff)
                        continue
                    if status >= 400:
                        body_snip = ""
                        try:
                            body_snip = (await resp.text())[:120]
                        except Exception:
                            body_snip = ""
                        last_err = f"HTTP {status} {body_snip}"
                        # 5xx：有限重試；4xx（非 429/418）：不重試
                        if status >= 500 and attempt + 1 < max_attempts_i:
                            backoff = compute_backoff_seconds(attempt, retry_after=None)
                            await _set_shared_cooldown(endpoint, backoff)
                            _log_http_event(
                                {
                                    "endpoint": endpoint,
                                    "status": status,
                                    "attempt": attempt + 1,
                                    "max_attempts_i": max_attempts_i,
                                    "result": "HTTP_5XX_BACKOFF",
                                    "final": False,
                                    "backoff_sec": backoff,
                                }
                            )
                            remain_after = budget - (time.time() - op_start)
                            if backoff > remain_after:
                                raise PublicHttpError(
                                    f"HTTP {status} budget for {endpoint}",
                                    endpoint=endpoint,
                                    status=status,
                                    attempts=attempt + 1,
                                )
                            await asyncio.sleep(backoff)
                            continue
                        _log_http_event(
                            {
                                "endpoint": endpoint,
                                "status": status,
                                "attempt": attempt + 1,
                                "max_attempts_i": max_attempts_i,
                                "result": "HTTP_ERROR",
                                "final": True,
                                "backoff_sec": None,
                            }
                        )
                        raise PublicHttpError(
                            f"HTTP {status} for {endpoint}",
                            endpoint=endpoint,
                            status=status,
                            attempts=attempt + 1,
                        )
                    data = await resp.json()
                    if attempt > 0:
                        _log_http_event(
                            {
                                "endpoint": endpoint,
                                "status": status,
                                "attempt": attempt + 1,
                                "max_attempts_i": max_attempts_i,
                                "result": "HTTP_OK_AFTER_RETRY",
                                "final": True,
                                "backoff_sec": None,
                            }
                        )
                    return data
            except EndpointBanned:
                raise
            except PublicHttpError:
                raise
            except asyncio.TimeoutError as exc:
                last_err = "timeout"
                last_status = None
                backoff = compute_backoff_seconds(attempt, retry_after=None)
                await _set_shared_cooldown(endpoint, min(backoff, 2.0))
                _log_http_event(
                    {
                        "endpoint": endpoint,
                        "status": None,
                        "attempt": attempt + 1,
                        "max_attempts_i": max_attempts_i,
                        "result": "HTTP_TIMEOUT",
                        "final": attempt + 1 >= max_attempts_i,
                        "backoff_sec": backoff,
                    }
                )
                if attempt + 1 >= max_attempts_i:
                    raise PublicHttpError(
                        f"timeout for {endpoint}",
                        endpoint=endpoint,
                        status=None,
                        attempts=attempt + 1,
                    ) from exc
                remain_after = budget - (time.time() - op_start)
                if backoff > remain_after:
                    raise PublicHttpError(
                        f"timeout budget for {endpoint}",
                        endpoint=endpoint,
                        status=None,
                        attempts=attempt + 1,
                    ) from exc
                await asyncio.sleep(min(backoff, remain_after))
                continue
            except aiohttp.ClientError as exc:
                last_err = type(exc).__name__
                backoff = compute_backoff_seconds(attempt, retry_after=None)
                _log_http_event(
                    {
                        "endpoint": endpoint,
                        "status": None,
                        "attempt": attempt + 1,
                        "max_attempts_i": max_attempts_i,
                        "result": "HTTP_CLIENT_ERROR",
                        "final": attempt + 1 >= max_attempts_i,
                        "backoff_sec": backoff,
                        "detail": last_err,
                    }
                )
                if attempt + 1 >= max_attempts_i:
                    raise PublicHttpError(
                        f"client error for {endpoint}: {last_err}",
                        endpoint=endpoint,
                        attempts=attempt + 1,
                    ) from exc
                remain_after = budget - (time.time() - op_start)
                if backoff > remain_after:
                    raise PublicHttpError(
                        f"client error budget for {endpoint}",
                        endpoint=endpoint,
                        attempts=attempt + 1,
                    ) from exc
                await asyncio.sleep(min(backoff, remain_after))
                continue

        raise PublicHttpError(
            f"exhausted attempts for {endpoint}: {last_err}",
            endpoint=endpoint,
            status=last_status,
            attempts=max_attempts_i,
        )


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


def _vault_path(symbol: str) -> Any:
    return config.KLINE_VAULT_DIR / f"{symbol}_1m.csv"


def annotate_bar_closed(df: pd.DataFrame, fetched_at_ms: int, *, interval: str = "1m") -> pd.DataFrame:
    """依「抓取當下」與 close_time 標 is_closed；不得事後用牆鐘把舊快照當完整版。"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["interval"] = interval
    if "close_time" not in out.columns:
        out["close_time"] = out["open_time"].astype("int64") + 59_999
    out["fetched_at_ms"] = int(fetched_at_ms)
    # 抓取當下若 close_time 已過 → 該列為交易所已收盤版本；否則為未完成快照
    out["is_closed"] = out["close_time"].astype("int64") < int(fetched_at_ms)
    return out


def merge_klines(
    cached: Optional[pd.DataFrame],
    fresh: pd.DataFrame,
    *,
    key_cols: tuple[str, ...] = ("open_time",),
) -> pd.DataFrame:
    """
    以 (open_time[, interval]) 合併 K 線版本：
    - 已確認收盤（is_closed=True）不可被未收盤／較舊快照覆蓋
    - 未收盤可被更新版（較新 fetched_at_ms，或已收盤版）取代
    - 兩邊皆已收盤時採較新 fetched_at_ms
    """
    if fresh is None or fresh.empty:
        return cached.copy() if cached is not None and not cached.empty else pd.DataFrame()
    if cached is None or cached.empty:
        return fresh.copy().sort_values("open_time").reset_index(drop=True)

    cols_prefer = [
        "open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
        "interval", "is_closed", "fetched_at_ms",
    ]
    a = cached.copy()
    b = fresh.copy()
    for df in (a, b):
        if "is_closed" not in df.columns:
            df["is_closed"] = False
        if "fetched_at_ms" not in df.columns:
            df["fetched_at_ms"] = 0
        if "interval" not in df.columns:
            df["interval"] = "1m"
        df["is_closed"] = df["is_closed"].astype(bool)
        df["fetched_at_ms"] = df["fetched_at_ms"].astype("int64")

    keys = [k for k in key_cols if k in a.columns and k in b.columns]
    if "interval" in a.columns and "interval" in b.columns and "interval" not in keys:
        keys = list(keys) + ["interval"]
    if not keys:
        keys = ["open_time"]

    by_key: dict[tuple, dict[str, Any]] = {}

    def _row_dict(row: pd.Series) -> dict[str, Any]:
        return {c: row[c] for c in cols_prefer if c in row.index}

    def _key(row: pd.Series) -> tuple:
        return tuple(row[k] for k in keys)

    def _better(new: dict[str, Any], old: dict[str, Any]) -> bool:
        n_closed = bool(new.get("is_closed"))
        o_closed = bool(old.get("is_closed"))
        if n_closed and not o_closed:
            return True
        if (not n_closed) and o_closed:
            return False  # 不可用未完成覆蓋已確認完成
        # 同狀態：較新觀測勝出
        return int(new.get("fetched_at_ms") or 0) >= int(old.get("fetched_at_ms") or 0)

    for _, row in a.iterrows():
        by_key[_key(row)] = _row_dict(row)
    for _, row in b.iterrows():
        k = _key(row)
        nd = _row_dict(row)
        if k not in by_key or _better(nd, by_key[k]):
            by_key[k] = nd

    out = pd.DataFrame(list(by_key.values()))
    if out.empty:
        return out
    return out.sort_values("open_time").reset_index(drop=True)


def load_vault(symbol: str) -> pd.DataFrame:
    p = _vault_path(symbol)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(p)
        return df
    except Exception:
        return pd.DataFrame()


def save_vault(symbol: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    # 已由 merge_klines 去重；此處只截尾，不再 keep-first drop_duplicates
    out = df.sort_values("open_time").tail(2000)
    p = _vault_path(symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(p, index=False)


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


def _parse_book_ticker_payload(data: Any, *, fetched_at: float) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
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


async def fetch_book_tickers(gs: GuardedSession) -> dict[str, dict[str, Any]]:
    """一般／掃描用 bookTicker（既有長預算）。"""
    data = await gs.get_json("/fapi/v1/ticker/bookTicker", purpose="scan_book")
    return _parse_book_ticker_payload(data, fetched_at=time.time())


async def fetch_book_tickers_manage(gs: GuardedSession) -> dict[str, dict[str, Any]]:
    """持倉管理專用短預算 bookTicker；失敗快速放棄，不繞過 429／418。"""
    data = await gs.get_json(
        "/fapi/v1/ticker/bookTicker",
        purpose="manage_book",
        op_budget_sec=float(config.MANAGE_BOOK_OP_BUDGET_SEC),
        max_attempts=int(config.MANAGE_BOOK_MAX_ATTEMPTS),
        request_timeout_sec=float(config.MANAGE_BOOK_REQUEST_TIMEOUT_SEC),
    )
    return _parse_book_ticker_payload(data, fetched_at=time.time())


async def fetch_klines_incremental(
    gs: GuardedSession,
    symbol: str,
    limit: int = config.KLINES_FETCH_LIMIT,
) -> tuple[pd.DataFrame, Optional[str]]:
    """增量合併 vault；失敗回傳 (舊資料或空, error)."""
    global _KLINE_SEM, _KLINE_LOCK
    if _KLINE_SEM is None:
        _KLINE_SEM = asyncio.Semaphore(config.KLINES_API_CONCURRENCY)
    if _KLINE_LOCK is None:
        _KLINE_LOCK = asyncio.Lock()

    cached = load_vault(symbol)
    try:
        async with _KLINE_SEM:
            await _rate_wait()
            raw = await gs.get_json(
                "/fapi/v1/klines",
                params={"symbol": symbol, "interval": "1m", "limit": limit},
            )
        fetched_at_ms = int(time.time() * 1000)
        rows = [
            {
                "open_time": int(x[0]),
                "open": float(x[1]),
                "high": float(x[2]),
                "low": float(x[3]),
                "close": float(x[4]),
                "volume": float(x[5]),
                "close_time": int(x[6]),
                "quote_volume": float(x[7]),
            }
            for x in raw
        ]
        fresh = annotate_bar_closed(pd.DataFrame(rows), fetched_at_ms, interval="1m")
        df = merge_klines(cached, fresh)
        save_vault(symbol, df)
        # run archive — 更新最新行情視圖；決策快照仍為不可變證據
        try:
            from .run_store import get_store

            store = get_store()
            if store is not None:
                store.archive_klines(symbol, df, note="incremental_fetch", fetched_at_ms=fetched_at_ms)
        except Exception:
            pass
        return df, None
    except Exception as exc:
        try:
            from .run_store import get_store

            store = get_store()
            if store is not None:
                store.note_quality("KLINE_FETCH_FAIL", symbol=symbol, detail=str(exc), replay_precise=False)
        except Exception:
            pass
        # 抓取失敗仍回傳既有資料供持倉管理；開倉側由訊號／暖機檢查擋下
        return cached if cached is not None else pd.DataFrame(), str(exc)


async def backfill_warmup_klines(
    gs: GuardedSession,
    symbols: list[str],
    *,
    min_bars: int = 100,
) -> dict[str, Any]:
    """
    觀測用：為本 run 歸檔補足至少 min_bars 根 1m（不改交易門檻）。
    對每個 symbol 額外拉 limit=max(min_bars+20, 150)。
    """
    from .run_store import get_store

    store = get_store()
    need = max(min_bars + 20, 150)
    summary: dict[str, Any] = {"requested": len(symbols), "ok": 0, "fail": 0, "short_after": []}
    for sym in symbols:
        df, err = await fetch_klines_incremental(gs, sym, limit=need)
        if err or df is None or df.empty:
            summary["fail"] += 1
            if store is not None:
                store.note_quality("WARMUP_BACKFILL_FAIL", symbol=sym, detail=str(err), replay_precise=False)
            continue
        if store is not None:
            meta = store.archive_klines(sym, df, note="warmup_backfill")
            if int(meta.get("bars") or 0) < min_bars:
                summary["short_after"].append({"symbol": sym, "bars": meta.get("bars")})
                store.note_quality(
                    "KLINE_WARMUP_SHORT",
                    symbol=sym,
                    detail=f"after_backfill bars={meta.get('bars')}<{min_bars}",
                    replay_precise=False,
                )
            else:
                summary["ok"] += 1
        else:
            summary["ok"] += 1
    return summary


def closed_klines(df: pd.DataFrame, now_ms: Optional[int] = None) -> pd.DataFrame:
    """
    僅回傳「抓取時已確認收盤」的 K。
    有 is_closed 欄時：必須 is_closed==True，且 close_time < now_ms。
    不可只因牆鐘已過一分鐘，就把未更新的早期快照當完整收盤 K。
    無 is_closed 的舊資料：保守地仍用 close_time < now_ms（legacy），但新 vault 必有 is_closed。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    now_ms = now_ms or int(time.time() * 1000)
    out = df
    if "close_time" in out.columns:
        time_ok = out["close_time"].astype("int64") < int(now_ms)
    else:
        time_ok = out["open_time"].astype("int64") + 60_000 <= int(now_ms)
    if "is_closed" in out.columns:
        mask = out["is_closed"].astype(bool) & time_ok
    else:
        mask = time_ok
    return out.loc[mask].reset_index(drop=True)


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
