"""Binance USD-M public aggTrade feed for H paper (event-time; no orders/keys).

Uses wss://fstream.binance.com/public/stream — public market data only.
Does not invent intradey volume from closed klines for the *current* bar.
Completed 3m klines may warm the RVOL baseline (finished bars only).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from . import config
from .bars import BarBuilder3m, SecondPriceTracker, TradePrint
from .price_series import SparseSecSeries
from .util import append_jsonl, now_iso, now_ts

# aggTrade lives on /market (bookTicker on /public). See Binance USD-M WS change notice.
MARKET_STREAM_BASE = "wss://fstream.binance.com/market/stream"
# Fallback legacy combined path (still widely used for aggTrade)
LEGACY_STREAM_BASE = "wss://fstream.binance.com/stream"
FAPI_BASE = "https://fapi.binance.com"

OnTradeCb = Callable[[str, TradePrint], Awaitable[None] | None]


@dataclass
class SymbolTape:
    symbol: str
    bars: BarBuilder3m = field(default_factory=lambda: BarBuilder3m(config.BAR_SEC))
    # Dense grid kept for Path A (legacy ER window); may carry within stale limit
    grid: SecondPriceTracker = field(
        default_factory=lambda: SecondPriceTracker(
            window=max(config.ER_WINDOW_SEC, 120),
            max_gap_carry_sec=config.TRADE_STALE_SEC_LEGACY,
        )
    )
    # Sparse series for Path B/C — no fill of missing seconds
    sparse: SparseSecSeries = field(
        default_factory=lambda: SparseSecSeries(max_keep_sec=360)
    )
    baseline_warmed: bool = False
    trade_count: int = 0
    last_trade_ts: float = 0.0
    last_eval_ts: float = 0.0
    last_eval_sec: int = -1


class BinancePublicAggTradeFeed:
    """Subscribe aggTrade for a dynamic symbol set; keep per-symbol event-time tapes."""

    def __init__(self, *, session: aiohttp.ClientSession, on_trade: Optional[OnTradeCb] = None):
        self.session = session
        self.on_trade = on_trade
        self.tapes: dict[str, SymbolTape] = {}
        self._wanted: set[str] = set()
        self._ws_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.connected = False
        self.last_msg_ts = 0.0
        self.reconnects = 0
        self.messages = 0
        self._lock = asyncio.Lock()

    def get_tape(self, symbol: str) -> SymbolTape:
        sym = symbol.upper()
        tape = self.tapes.get(sym)
        if tape is None:
            tape = SymbolTape(symbol=sym)
            self.tapes[sym] = tape
        return tape

    async def set_symbols(self, symbols: list[str] | set[str]) -> None:
        wanted = {str(s).upper() for s in symbols if s}
        async with self._lock:
            added = wanted - self._wanted
            self._wanted = wanted
        for sym in sorted(added):
            await self.warm_baseline(sym)
        # force reconnect if set changed materially
        if self._ws_task and not self._ws_task.done():
            # signal reconnect by closing — loop will rebuild URL
            self.reconnects += 0  # no-op marker
            # store flag; runner checks wanted hash
            self._symbols_dirty = True  # type: ignore[attr-defined]
        else:
            self._symbols_dirty = True  # type: ignore[attr-defined]

    async def warm_baseline(self, symbol: str) -> None:
        """Load last N *completed* 3m quote volumes from REST klines (not current bar)."""
        tape = self.get_tape(symbol)
        if tape.baseline_warmed and len(tape.bars.completed) >= config.RVOL_LOOKBACK_BARS:
            return
        url = f"{FAPI_BASE}/fapi/v1/klines"
        params = {
            "symbol": symbol.upper(),
            "interval": "3m",
            "limit": config.RVOL_LOOKBACK_BARS + 2,
        }
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:  # noqa: BLE001
            append_jsonl(
                config.shared_paths()["scan_log"],
                {"event": "BASELINE_WARM_FAIL", "symbol": symbol, "error": str(exc), "at": now_iso()},
            )
            return
        # drop last candle if it is still forming
        now_ms = int(time.time() * 1000)
        completed = []
        for row in rows:
            open_ms = int(row[0])
            close_ms = int(row[6])
            if close_ms >= now_ms:
                continue  # forming — do not use as complete baseline
            qv = float(row[7])  # quote asset volume
            start = open_ms / 1000.0
            end = close_ms / 1000.0 + 0.001
            from .bars import Bar3m

            completed.append(
                Bar3m(
                    start_ts=start,
                    end_ts=end,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    quote_volume=qv,
                    trade_count=int(row[8] or 0),
                    complete=True,
                )
            )
        if completed:
            tape.bars.completed = completed[-config.RVOL_LOOKBACK_BARS :]
            tape.baseline_warmed = True
            append_jsonl(
                config.shared_paths()["scan_log"],
                {
                    "event": "BASELINE_WARMED",
                    "symbol": symbol,
                    "n": len(tape.bars.completed),
                    "median": tape.bars.baseline_median(config.RVOL_LOOKBACK_BARS),
                    "at": now_iso(),
                },
            )

    def _urls(self) -> list[str]:
        syms = sorted(self._wanted)
        if not syms:
            return []
        parts = [f"{s.lower()}@aggTrade" for s in syms]
        q = "/".join(parts)
        return [
            f"{MARKET_STREAM_BASE}?streams={q}",
            f"{LEGACY_STREAM_BASE}?streams={q}",
        ]

    def ingest_agg(self, payload: dict[str, Any]) -> Optional[TradePrint]:
        """Parse combined or raw aggTrade payload → TradePrint."""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not data:
            return None
        try:
            # T = trade time (event), p = price, q = qty
            ts = float(data.get("T") or data.get("E") or 0) / 1000.0
            px = float(data["p"])
            qty = float(data["q"])
        except Exception:
            return None
        if ts <= 0 or px <= 0 or qty <= 0:
            return None
        return TradePrint(event_ts=ts, price=px, qty=qty, quote=px * qty)

    async def _handle_msg(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        stream = str(payload.get("stream") or "")
        data = payload.get("data") if "data" in payload else payload
        sym = ""
        if stream and "@" in stream:
            sym = stream.split("@", 1)[0].upper()
        if not sym and isinstance(data, dict):
            sym = str(data.get("s") or "").upper()
        if not sym or sym not in self._wanted:
            return
        tr = self.ingest_agg(payload if "data" in payload else {"data": data})
        if tr is None:
            return
        tape = self.get_tape(sym)
        tape.bars.on_trade(tr)
        tape.grid.on_trade(tr)
        tape.sparse.on_trade(tr.event_ts, tr.price)
        tape.trade_count += 1
        tape.last_trade_ts = tr.event_ts
        self.messages += 1
        self.last_msg_ts = now_ts()
        if self.on_trade:
            res = self.on_trade(sym, tr)
            if asyncio.iscoroutine(res):
                await res

    async def run_forever(self) -> None:
        self._stop.clear()
        self._symbols_dirty = True  # type: ignore[attr-defined]
        url_i = 0
        while not self._stop.is_set():
            urls = self._urls()
            if not urls:
                self.connected = False
                await asyncio.sleep(1.0)
                continue
            url = urls[url_i % len(urls)]
            self._symbols_dirty = False  # type: ignore[attr-defined]
            got_msg = False
            try:
                async with self.session.ws_connect(
                    url,
                    heartbeat=20,
                    autoclose=True,
                    autoping=True,
                ) as ws:
                    self.connected = True
                    host = url.split("?")[0]
                    print(
                        f"[成交串流] 已連線｜追蹤 {len(self._wanted)} 幣｜{host}",
                        flush=True,
                    )
                    idle = 0.0
                    while not self._stop.is_set():
                        if getattr(self, "_symbols_dirty", False):
                            break
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                        except asyncio.TimeoutError:
                            idle += 5.0
                            # no trades for 45s → try alternate endpoint
                            if idle >= 45.0 and not got_msg:
                                print("[成交串流] 45s 無成交訊息，切換端點重連", flush=True)
                                break
                            continue
                        idle = 0.0
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            got_msg = True
                            await self._handle_msg(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.reconnects += 1
                append_jsonl(
                    config.shared_paths()["scan_log"],
                    {
                        "event": "TRADE_WS_RECONNECT",
                        "error": str(exc),
                        "n": self.reconnects,
                        "url": url,
                        "at": now_iso(),
                    },
                )
                print(f"[成交串流] 斷線重連 #{self.reconnects}：{exc}", flush=True)
                await asyncio.sleep(min(2 + self.reconnects, 15))
            finally:
                self.connected = False
            if not got_msg:
                url_i += 1

    def start(self) -> asyncio.Task:
        if self._ws_task and not self._ws_task.done():
            return self._ws_task
        self._ws_task = asyncio.create_task(self.run_forever(), name="h_aggtrade_feed")
        return self._ws_task

    async def stop(self) -> None:
        self._stop.set()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except Exception:
                pass
