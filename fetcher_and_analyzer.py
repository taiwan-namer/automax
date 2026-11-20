"""
記憶體即抓即算引擎 — 純股性與莊家控盤行為解剖

流程：
  1. 篩選純加密貨幣小幣 → 取 24h 漲幅/跌幅各 Top 10（共 20 支）
  2. 抓取近 3 天 1m / 15m K 線至 RAM
  3. 計算 A～F 六大客觀特徵
  4. 立刻釋放該幣 K 線記憶體，僅寫入一行統計摘要
"""

from __future__ import annotations

import asyncio
import gc
import time
from pathlib import Path
from typing import Any, Callable

import aiohttp
import numpy as np
import pandas as pd

import config

ProgressCallback = Callable[[int, int, str, str], None]

# CSV 欄位（固定順序）
SUMMARY_COLUMNS: list[str] = [
    "symbol",
    "board",  # gainer / loser
    "rank",
    # A. 當前盤勢位置
    "price_change_pct_24h",
    "quote_volume_24h_wan",
    "price_position_pct",
    # B. 波動與節奏
    "atr_1m_pct",
    "atr_15m_pct",
    "pulse_index",
    # C. 插針與洗盤
    "avg_upper_wick_15m",
    "avg_lower_wick_15m",
    "long_wick_freq",
    # D. 爆量後走向（3 / 5 / 15 分鐘）
    "spike_count",
    "cont_rate_3m",
    "cont_avg_pct_3m",
    "rev_rate_3m",
    "rev_avg_pct_3m",
    "cont_rate_5m",
    "cont_avg_pct_5m",
    "rev_rate_5m",
    "rev_avg_pct_5m",
    "cont_rate_15m",
    "cont_avg_pct_15m",
    "rev_rate_15m",
    "rev_avg_pct_15m",
    # E. 連續順勢極限
    "streak3_cont_rate",
    "streak3_rev_rate",
    "streak3_avg_rev_pct",
    "streak4_cont_rate",
    "streak4_rev_rate",
    "streak4_avg_rev_pct",
    # F. 量能變化
    "recent_volume_ratio",
]


# ---------------------------------------------------------------------------
# 幣種篩選輔助
# ---------------------------------------------------------------------------


def _normalize_base(base_asset: str) -> str:
    """去掉 1000 / 1000000 等倍數前綴，例如 1000PEPE → PEPE。"""
    base = base_asset.upper()
    for prefix in ("1000000", "100000", "10000", "1000"):
        if base.startswith(prefix) and len(base) > len(prefix):
            rest = base[len(prefix) :]
            if rest and rest[0].isalpha():
                return rest
    return base


def _is_tradfi_or_major(symbol: str, base_asset: str) -> bool:
    """關鍵字黑名單：美股／TradFi／外匯／商品／主流大幣。"""
    base = base_asset.upper()
    norm = _normalize_base(base)
    sym = symbol.upper()

    if norm in config.EXCLUDED_MAJOR_BASES or base in config.EXCLUDED_MAJOR_BASES:
        return True
    if norm in config.TRADFI_BASE_BLACKLIST or base in config.TRADFI_BASE_BLACKLIST:
        return True
    for prefix in config.TRADFI_SYMBOL_PREFIXES:
        if sym.startswith(prefix) or base.startswith(prefix) or norm.startswith(prefix):
            return True
    return False


def _has_non_crypto_subtype(subtypes: Any) -> bool:
    if not subtypes:
        return False
    items = subtypes if isinstance(subtypes, list) else [subtypes]
    for item in items:
        text = str(item)
        for kw in config.NON_CRYPTO_SUBTYPE_KEYWORDS:
            if kw.lower() in text.lower():
                return True
    return False


def _is_pure_crypto_perp(symbol_info: dict[str, Any]) -> bool:
    """嚴格篩選：USDT 永續 + COIN + 非 TradFi/商品/外匯 + 非主流大幣。"""
    if symbol_info.get("contractType") != "PERPETUAL":
        return False
    if symbol_info.get("quoteAsset") != "USDT":
        return False
    if symbol_info.get("status") != "TRADING":
        return False
    if symbol_info.get("underlyingType") != config.REQUIRED_UNDERLYING_TYPE:
        return False
    if _has_non_crypto_subtype(symbol_info.get("underlyingSubType")):
        return False
    symbol = str(symbol_info.get("symbol", ""))
    base = str(symbol_info.get("baseAsset", ""))
    if _is_tradfi_or_major(symbol, base):
        return False
    return True


# ---------------------------------------------------------------------------
# 選幣：漲幅榜 + 跌幅榜
# ---------------------------------------------------------------------------


async def fetch_hot_symbols(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    """
    回傳共 20 支：漲幅 Top10 + 跌幅 Top10。
    每筆含 symbol / board / rank / ticker 欄位。
    """
    base_url = config.BINANCE_FUTURES_BASE_URL

    async with session.get(f"{base_url}/fapi/v1/exchangeInfo") as resp:
        resp.raise_for_status()
        exchange_info = await resp.json()

    pure_crypto: set[str] = set()
    for s in exchange_info.get("symbols", []):
        if _is_pure_crypto_perp(s):
            pure_crypto.add(s["symbol"])

    async with session.get(f"{base_url}/fapi/v1/ticker/24hr") as resp:
        resp.raise_for_status()
        tickers = await resp.json()

    candidates: list[dict[str, Any]] = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if symbol not in pure_crypto:
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            change = float(t.get("priceChangePercent", 0))
            last = float(t.get("lastPrice", 0))
            high = float(t.get("highPrice", 0))
            low = float(t.get("lowPrice", 0))
        except (TypeError, ValueError):
            continue
        if qv < config.MIN_QUOTE_VOLUME_USDT:
            continue
        if last <= 0 or high <= 0 or low <= 0:
            continue
        candidates.append(
            {
                "symbol": symbol,
                "price_change_pct_24h": change,
                "quote_volume_24h": qv,
                "last_price": last,
                "high_24h": high,
                "low_24h": low,
            }
        )

    if not candidates:
        return []

    by_change = sorted(candidates, key=lambda x: x["price_change_pct_24h"], reverse=True)
    gainers = by_change[: config.TOP_GAINERS]
    losers = sorted(candidates, key=lambda x: x["price_change_pct_24h"])[: config.TOP_LOSERS]

    # 避免同一幣同時進兩邊（極端情況）
    gainer_syms = {g["symbol"] for g in gainers}
    losers = [x for x in losers if x["symbol"] not in gainer_syms][: config.TOP_LOSERS]

    result: list[dict[str, Any]] = []
    for i, g in enumerate(gainers, start=1):
        result.append({**g, "board": "gainer", "rank": i})
    for i, lo in enumerate(losers, start=1):
        result.append({**lo, "board": "loser", "rank": i})
    return result


# ---------------------------------------------------------------------------
# K 線抓取（RAM + 本地增量快取，防 Rate Limit）
# ---------------------------------------------------------------------------

_KLINE_MEM_CACHE: dict[tuple[str, str], pd.DataFrame] = {}
_KLINE_API_SEM: asyncio.Semaphore | None = None
_KLINE_API_LOCK: asyncio.Lock | None = None
_KLINE_LAST_REQ_TS: float = 0.0

_KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "quote_volume"]
_KLINE_RAW_COLS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]


def _kline_cache_path(symbol: str, interval: str) -> Path:
    """正式路徑：data/kline_vault/{SYMBOL}_{interval}.csv"""
    base = Path(getattr(config, "KLINES_VAULT_DIR", config.KLINES_CACHE_DIR))
    base.mkdir(parents=True, exist_ok=True)
    safe = symbol.replace("/", "_")
    return base / f"{safe}_{interval}.csv"


def _empty_klines() -> pd.DataFrame:
    return pd.DataFrame(columns=_KLINE_COLS)


def _normalize_klines(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_klines()
    out = df.copy()
    for col in _KLINE_COLS:
        if col not in out.columns:
            out[col] = np.nan
    out = out[_KLINE_COLS]
    out = out.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["open_time"] = out["open_time"].astype(np.int64)
    return out


def _load_kline_cache(symbol: str, interval: str) -> pd.DataFrame:
    """先讀 RAM，再讀本地 CSV 增量快取。"""
    key = (symbol, interval)
    if key in _KLINE_MEM_CACHE and not _KLINE_MEM_CACHE[key].empty:
        return _normalize_klines(_KLINE_MEM_CACHE[key])
    path = _kline_cache_path(symbol, interval)
    if path.exists() and path.stat().st_size > 0:
        try:
            df = pd.read_csv(path)
            df = _normalize_klines(df)
            _KLINE_MEM_CACHE[key] = df
            return df
        except Exception:
            return _empty_klines()
    return _empty_klines()


def load_klines_from_vault(symbol: str, interval: str) -> pd.DataFrame:
    """對外唯讀：從 kline_vault／記憶體載入 K 線（失敗回傳空表）。"""
    try:
        if not symbol:
            return _empty_klines()
        return _load_kline_cache(symbol, interval)
    except Exception:
        return _empty_klines()


def _save_kline_cache(symbol: str, interval: str, df: pd.DataFrame) -> None:
    """寫入 RAM + 本地 CSV（僅精簡欄位，供下次增量續傳）。"""
    key = (symbol, interval)
    clean = _normalize_klines(df)
    _KLINE_MEM_CACHE[key] = clean
    try:
        path = _kline_cache_path(symbol, interval)
        clean.to_csv(path, index=False)
    except Exception:
        pass


def _interval_window(interval: str) -> tuple[int, int]:
    """
    回傳 (start_ms 對應的回溯毫秒, max_bars)。
    1m → 24h / 1440 根；15m → 7 天 / 最多 1000 根。
    """
    if interval in ("1m", "1M"):
        ms = config.LOOKBACK_1M_HOURS * 60 * 60 * 1000
        return ms, config.LOOKBACK_1M_BARS
    if interval in ("15m", "15M"):
        ms = config.LOOKBACK_15M_DAYS * 24 * 60 * 60 * 1000
        return ms, config.LOOKBACK_15M_MAX_BARS
    # 其他週期：退回 LOOKBACK_DAYS
    ms = config.LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    return ms, config.KLINES_LIMIT


def _trim_window(
    df: pd.DataFrame,
    lookback_ms: int,
    max_bars: int,
    end_ms: int | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = end_ms - lookback_ms
    out = df[df["open_time"] >= start_ms].reset_index(drop=True)
    if max_bars > 0 and len(out) > max_bars:
        out = out.tail(max_bars).reset_index(drop=True)
    return out


def _ensure_api_guards() -> tuple[asyncio.Semaphore, asyncio.Lock]:
    global _KLINE_API_SEM, _KLINE_API_LOCK
    if _KLINE_API_SEM is None:
        _KLINE_API_SEM = asyncio.Semaphore(config.KLINES_API_CONCURRENCY)
    if _KLINE_API_LOCK is None:
        _KLINE_API_LOCK = asyncio.Lock()
    return _KLINE_API_SEM, _KLINE_API_LOCK


async def _fetch_klines_chunk(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[Any]]:
    """單次 K 線請求，附併發上限 + 最小間隔（遵守 Rate Limit）。"""
    global _KLINE_LAST_REQ_TS
    sem, lock = _ensure_api_guards()
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": config.KLINES_LIMIT,
    }
    url = f"{config.BINANCE_FUTURES_BASE_URL}/fapi/v1/klines"
    async with sem:
        async with lock:
            now = time.time()
            wait = config.KLINES_MIN_INTERVAL_SEC - (now - _KLINE_LAST_REQ_TS)
            if wait > 0:
                await asyncio.sleep(wait)
            _KLINE_LAST_REQ_TS = time.time()
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if isinstance(data, dict) and "code" in data:
                raise RuntimeError(f"Binance API error for {symbol}: {data}")
            return data


async def fetch_klines_df(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    lookback_days: int | None = None,
    max_bars: int | None = None,
) -> pd.DataFrame:
    """
    增量抓取 K 線（寫入 data/kline_vault/）：
      1) 讀取本地／記憶體金庫
      2) 只向 API 請求「最後一筆 open_time 之後」的新 K 線
      3) 以 open_time 去重合併，裁切視窗後覆寫保存
    lookback_days 若提供則覆寫預設時間窗（相容舊呼叫）。
    """
    end_ms = int(time.time() * 1000)
    default_ms, default_max = _interval_window(interval)
    if lookback_days is not None:
        lookback_ms = int(lookback_days * 24 * 60 * 60 * 1000)
    else:
        lookback_ms = default_ms
    max_keep = max_bars if max_bars is not None else default_max
    start_ms = end_ms - lookback_ms

    cached = _load_kline_cache(symbol, interval)
    cached = _trim_window(cached, lookback_ms, max_keep, end_ms)

    # 增量起點：金庫最後一根 + 1；若頭部仍缺 lookback 起點則補抓缺口
    if not cached.empty:
        last_open = int(cached["open_time"].iloc[-1])
        first_open = int(cached["open_time"].iloc[0])
        if first_open > start_ms + 60_000:
            cursor = start_ms
        else:
            cursor = last_open + 1
    else:
        cursor = start_ms

    all_rows: list[list[Any]] = []
    while cursor < end_ms:
        chunk = await _fetch_klines_chunk(session, symbol, interval, cursor, end_ms)
        if not chunk:
            break
        all_rows.extend(chunk)
        last_open = int(chunk[-1][0])
        next_cursor = last_open + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(chunk) < config.KLINES_LIMIT:
            break

    if all_rows:
        new_df = pd.DataFrame(all_rows, columns=_KLINE_RAW_COLS)
        new_df = _normalize_klines(new_df)
        if cached.empty:
            merged = new_df
        else:
            merged = _normalize_klines(pd.concat([cached, new_df], ignore_index=True))
    else:
        merged = cached

    merged = _trim_window(merged, lookback_ms, max_keep, end_ms)
    _save_kline_cache(symbol, interval, merged)
    return merged.copy()


# ---------------------------------------------------------------------------
# A～F 特徵計算
# ---------------------------------------------------------------------------


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b == 0 or np.isnan(b) or np.isnan(a):
        return default
    return float(a / b)


def calc_section_a(meta: dict[str, Any]) -> dict[str, float]:
    """A. 當前盤勢位置（來自 24h ticker，無需 K 線）。"""
    high = float(meta["high_24h"])
    low = float(meta["low_24h"])
    last = float(meta["last_price"])
    rng = high - low
    position = _safe_div(last - low, rng, default=0.5) * 100.0
    position = float(np.clip(position, 0.0, 100.0))
    return {
        "price_change_pct_24h": round(float(meta["price_change_pct_24h"]), 4),
        "quote_volume_24h_wan": round(float(meta["quote_volume_24h"]) / 10_000.0, 2),
        "price_position_pct": round(position, 2),
    }


def calc_section_b(df_1m: pd.DataFrame, df_15m: pd.DataFrame) -> dict[str, float]:
    """B. 波動與節奏（ATR + 脈衝指數）。"""
    atr_1m = 0.0
    atr_15m = 0.0
    if not df_1m.empty:
        rng = (df_1m["high"] - df_1m["low"]) / df_1m["close"].replace(0, np.nan)
        atr_1m = float(rng.mean(skipna=True) * 100.0)
    if not df_15m.empty:
        rng = (df_15m["high"] - df_15m["low"]) / df_15m["close"].replace(0, np.nan)
        atr_15m = float(rng.mean(skipna=True) * 100.0)

    # 脈衝指數：過去 24h（最多 96 根 15m）前 5 大 |漲跌| 佔總變動比
    pulse = 0.0
    if len(df_15m) > 0:
        recent = df_15m.tail(config.PULSE_LOOKBACK_15M_BARS)
        body_pct = ((recent["close"] - recent["open"]) / recent["open"].replace(0, np.nan)).abs()
        body_pct = body_pct.dropna()
        total = float(body_pct.sum())
        if total > 0:
            top = body_pct.nlargest(min(config.PULSE_TOP_N, len(body_pct))).sum()
            pulse = float(top / total)

    return {
        "atr_1m_pct": round(atr_1m if not np.isnan(atr_1m) else 0.0, 4),
        "atr_15m_pct": round(atr_15m if not np.isnan(atr_15m) else 0.0, 4),
        "pulse_index": round(pulse, 4),
    }


def calc_section_c(df_15m: pd.DataFrame) -> dict[str, float]:
    """C. 插針與洗盤習性。"""
    if df_15m.empty:
        return {"avg_upper_wick_15m": 0.0, "avg_lower_wick_15m": 0.0, "long_wick_freq": 0.0}

    high = df_15m["high"].to_numpy(dtype=float)
    low = df_15m["low"].to_numpy(dtype=float)
    open_ = df_15m["open"].to_numpy(dtype=float)
    close = df_15m["close"].to_numpy(dtype=float)
    rng = high - low
    body = np.abs(close - open_)
    upper = high - np.maximum(open_, close)
    lower = np.minimum(open_, close) - low

    valid = rng > 0
    if not np.any(valid):
        return {"avg_upper_wick_15m": 0.0, "avg_lower_wick_15m": 0.0, "long_wick_freq": 0.0}

    avg_upper = float(np.mean(upper[valid] / rng[valid]))
    avg_lower = float(np.mean(lower[valid] / rng[valid]))

    # 長針：單側較長影線 > 實體 × 2
    wick_max = np.maximum(upper, lower)
    long_mask = valid & (body > 0) & (wick_max > body * config.LONG_WICK_BODY_MULTIPLIER)
    # 實體極小也視為長針候選（十字星長針）
    doji_long = valid & (body <= 0) & (wick_max > 0)
    long_freq = float(np.mean(long_mask | doji_long))

    return {
        "avg_upper_wick_15m": round(avg_upper, 4),
        "avg_lower_wick_15m": round(avg_lower, 4),
        "long_wick_freq": round(long_freq, 4),
    }


def calc_section_d(df_1m: pd.DataFrame) -> dict[str, float]:
    """
    D. 爆量後真實走向（不設停利停損）。
    爆量：成交量 > 前 20 根均量 × 5。
    """
    empty = {
        "spike_count": 0,
        "cont_rate_3m": 0.0,
        "cont_avg_pct_3m": 0.0,
        "rev_rate_3m": 0.0,
        "rev_avg_pct_3m": 0.0,
        "cont_rate_5m": 0.0,
        "cont_avg_pct_5m": 0.0,
        "rev_rate_5m": 0.0,
        "rev_avg_pct_5m": 0.0,
        "cont_rate_15m": 0.0,
        "cont_avg_pct_15m": 0.0,
        "rev_rate_15m": 0.0,
        "rev_avg_pct_15m": 0.0,
    }
    if len(df_1m) < config.VOLUME_MA_PERIOD + max(config.SPIKE_HORIZONS_1M) + 2:
        return empty

    vol = df_1m["volume"].to_numpy(dtype=float)
    open_ = df_1m["open"].to_numpy(dtype=float)
    high = df_1m["high"].to_numpy(dtype=float)
    low = df_1m["low"].to_numpy(dtype=float)
    close = df_1m["close"].to_numpy(dtype=float)
    n = len(df_1m)

    vol_ma = (
        pd.Series(vol)
        .shift(1)
        .rolling(config.VOLUME_MA_PERIOD, min_periods=config.VOLUME_MA_PERIOD)
        .mean()
        .to_numpy()
    )
    body = close - open_
    spike_idx = np.where((vol_ma > 0) & (vol > vol_ma * config.VOLUME_SPIKE_MULTIPLIER) & (body != 0))[0]
    if len(spike_idx) == 0:
        return empty

    stats: dict[int, dict[str, list[float]]] = {
        h: {"cont": [], "cont_pct": [], "rev": [], "rev_pct": []} for h in config.SPIKE_HORIZONS_1M
    }

    for i in spike_idx:
        direction = 1 if body[i] > 0 else -1
        entry_close = close[i]
        entry_open = open_[i]
        if entry_close <= 0 or entry_open <= 0:
            continue

        for h in config.SPIKE_HORIZONS_1M:
            j = i + h
            if j >= n:
                continue
            # 延續：第 h 分鐘收盤相對爆量收盤，仍往同方向走
            move_from_close = (close[j] - entry_close) / entry_close
            same_dir = (move_from_close * direction) > 0
            stats[h]["cont"].append(1.0 if same_dir else 0.0)
            if same_dir:
                stats[h]["cont_pct"].append(abs(move_from_close) * 100.0)

            # 反轉：視窗內曾反向穿越爆量 K 線開盤價
            win_high = high[i + 1 : j + 1]
            win_low = low[i + 1 : j + 1]
            if direction > 0:
                reversed_ = bool(np.any(win_low <= entry_open))
                if reversed_:
                    # 反向幅度：從開盤價往下走到最低
                    rev_pct = (entry_open - float(np.min(win_low))) / entry_open * 100.0
                    stats[h]["rev_pct"].append(max(rev_pct, 0.0))
            else:
                reversed_ = bool(np.any(win_high >= entry_open))
                if reversed_:
                    rev_pct = (float(np.max(win_high)) - entry_open) / entry_open * 100.0
                    stats[h]["rev_pct"].append(max(rev_pct, 0.0))
            stats[h]["rev"].append(1.0 if reversed_ else 0.0)

    out = {"spike_count": int(len(spike_idx))}
    for h in config.SPIKE_HORIZONS_1M:
        cont = stats[h]["cont"]
        rev = stats[h]["rev"]
        out[f"cont_rate_{h}m"] = round(float(np.mean(cont)) * 100.0, 2) if cont else 0.0
        out[f"cont_avg_pct_{h}m"] = (
            round(float(np.mean(stats[h]["cont_pct"])), 4) if stats[h]["cont_pct"] else 0.0
        )
        out[f"rev_rate_{h}m"] = round(float(np.mean(rev)) * 100.0, 2) if rev else 0.0
        out[f"rev_avg_pct_{h}m"] = (
            round(float(np.mean(stats[h]["rev_pct"])), 4) if stats[h]["rev_pct"] else 0.0
        )
    return out


def calc_section_e(df_15m: pd.DataFrame) -> dict[str, float]:
    """E. 連續順勢極限：連 3 / 連 4 後下一根繼續 vs 反轉。"""
    result = {
        "streak3_cont_rate": 0.0,
        "streak3_rev_rate": 0.0,
        "streak3_avg_rev_pct": 0.0,
        "streak4_cont_rate": 0.0,
        "streak4_rev_rate": 0.0,
        "streak4_avg_rev_pct": 0.0,
    }
    if len(df_15m) < 5:
        return result

    open_ = df_15m["open"].to_numpy(dtype=float)
    close = df_15m["close"].to_numpy(dtype=float)
    # +1 紅 / -1 黑 / 0 十字
    direction = np.sign(close - open_)
    n = len(df_15m)

    for streak_len in config.STREAK_LENGTHS:
        cont_flags: list[float] = []
        rev_flags: list[float] = []
        rev_pcts: list[float] = []

        for i in range(streak_len - 1, n - 1):
            window = direction[i - streak_len + 1 : i + 1]
            if np.any(window == 0):
                continue
            if not np.all(window == window[0]):
                continue
            streak_dir = int(window[0])
            nxt = direction[i + 1]
            if nxt == 0:
                continue
            if nxt == streak_dir:
                cont_flags.append(1.0)
                rev_flags.append(0.0)
            else:
                cont_flags.append(0.0)
                rev_flags.append(1.0)
                # 反轉回調幅度：下一根相對其開盤的逆向幅度
                o = open_[i + 1]
                c = close[i + 1]
                if o > 0:
                    rev_pcts.append(abs(c - o) / o * 100.0)

        prefix = f"streak{streak_len}"
        if cont_flags:
            result[f"{prefix}_cont_rate"] = round(float(np.mean(cont_flags)) * 100.0, 2)
            result[f"{prefix}_rev_rate"] = round(float(np.mean(rev_flags)) * 100.0, 2)
            result[f"{prefix}_avg_rev_pct"] = (
                round(float(np.mean(rev_pcts)), 4) if rev_pcts else 0.0
            )
    return result


def calc_section_f(df_1m: pd.DataFrame) -> dict[str, float]:
    """F. 量能變化趨勢：近 2h 均量 / 過去 24h 均量。"""
    if df_1m.empty:
        return {"recent_volume_ratio": 0.0}
    vol = df_1m["volume"]
    recent = vol.tail(config.RECENT_VOLUME_BARS_1M)
    baseline = vol.tail(config.BASELINE_VOLUME_BARS_1M)
    base_mean = float(baseline.mean()) if len(baseline) else 0.0
    recent_mean = float(recent.mean()) if len(recent) else 0.0
    ratio = _safe_div(recent_mean, base_mean, default=0.0)
    return {"recent_volume_ratio": round(ratio, 4)}


# ---------------------------------------------------------------------------
# 單幣分析
# ---------------------------------------------------------------------------


async def analyze_symbol(
    session: aiohttp.ClientSession,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """抓 K 線 → 計算 A～F → 釋放記憶體 → 回傳單行 dict。"""
    symbol = meta["symbol"]
    df_1m: pd.DataFrame | None = None
    df_15m: pd.DataFrame | None = None

    try:
        # 1m：過去 24h（1440 根）；15m：過去 7 天（≤1000 根）— 均走增量金庫
        df_1m = await fetch_klines_df(
            session,
            symbol,
            "1m",
            lookback_days=config.LOOKBACK_1M_HOURS / 24.0,
            max_bars=config.LOOKBACK_1M_BARS,
        )
        df_15m = await fetch_klines_df(
            session,
            symbol,
            "15m",
            lookback_days=config.LOOKBACK_15M_DAYS,
            max_bars=config.LOOKBACK_15M_MAX_BARS,
        )

        row: dict[str, Any] = {
            "symbol": symbol,
            "board": meta["board"],
            "rank": meta["rank"],
        }
        row.update(calc_section_a(meta))
        row.update(calc_section_b(df_1m, df_15m))
        row.update(calc_section_c(df_15m))
        row.update(calc_section_d(df_1m))
        row.update(calc_section_e(df_15m))
        row.update(calc_section_f(df_1m))
        return row
    finally:
        if df_1m is not None:
            del df_1m
        if df_15m is not None:
            del df_15m
        gc.collect()


def _write_summary(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([{c: r.get(c) for c in SUMMARY_COLUMNS} for r in rows])
    # 漲幅榜在前、跌幅榜在後；各自依 rank
    board_order = {"gainer": 0, "loser": 1}
    df["_ord"] = df["board"].map(board_order)
    df = df.sort_values(["_ord", "rank"]).drop(columns=["_ord"])
    df.to_csv(csv_path, index=False)


async def run_analysis(progress_callback: ProgressCallback | None = None) -> Path:
    """主流程：選 Top50（漲/跌各 25）當紅小幣 → 解剖 → 覆寫寫入 CSV。"""
    out_path = Path(config.SUMMARY_CSV)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    timeout = aiohttp.ClientTimeout(total=120)
    connector = aiohttp.TCPConnector(limit=config.CONCURRENT_SYMBOLS + 2)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        hot = await fetch_hot_symbols(session)
        total = len(hot)
        if progress_callback:
            progress_callback(
                0,
                total,
                "-",
                f"已篩出純加密小幣漲跌榜共 {total} 支（漲 {sum(1 for x in hot if x['board']=='gainer')} / "
                f"跌 {sum(1 for x in hot if x['board']=='loser')}）",
            )

        if total == 0:
            _write_summary(out_path, [])
            return out_path

        sem = asyncio.Semaphore(config.CONCURRENT_SYMBOLS)
        rows: list[dict[str, Any]] = []
        completed = 0
        lock = asyncio.Lock()

        async def _worker(item: dict[str, Any]) -> None:
            nonlocal completed
            symbol = item["symbol"]
            async with sem:
                if progress_callback:
                    async with lock:
                        progress_callback(
                            min(completed + 1, total),
                            total,
                            symbol,
                            f"正在解剖 {symbol}（{item['board']} #{item['rank']}）...",
                        )
                try:
                    row = await analyze_symbol(session, item)
                    async with lock:
                        rows.append(row)
                        # 每完成一幣即覆寫 CSV（單次執行內的進度保存）
                        _write_summary(out_path, rows)
                        completed += 1
                        if progress_callback:
                            progress_callback(
                                completed,
                                total,
                                symbol,
                                f"正在解剖 {symbol}... 完成，記憶體已釋放",
                            )
                except Exception as exc:
                    async with lock:
                        completed += 1
                        if progress_callback:
                            progress_callback(
                                completed,
                                total,
                                symbol,
                                f"正在解剖 {symbol}... 失敗：{exc}",
                            )
                finally:
                    # Top50 壓力測試：每支幣之間間隔，避免打爆幣安 Rate Limit
                    delay = float(getattr(config, "KLINES_PER_SYMBOL_DELAY_SEC", 0.05))
                    if delay > 0:
                        await asyncio.sleep(delay)

        await asyncio.gather(*[_worker(item) for item in hot])

    _write_summary(out_path, rows)
    return out_path
