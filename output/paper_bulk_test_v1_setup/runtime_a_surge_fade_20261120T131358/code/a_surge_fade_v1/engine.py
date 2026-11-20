"""Paper loop: manage-first, then scan surge-fade shorts into three isolated arms."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import aiohttp

from . import config
from .market import GuardedSession, fetch_book_tickers, fetch_completed_1m_bars, refresh_universe
from .portfolio import load_arm, manage_position, process_pending, queue_short_limit, save_arm
from .signals import SymbolState, reset_after_trade_cycle, step_state
from .util import append_jsonl, atomic_write_json, ensure_dirs, now_iso, now_ts


def _load_observer() -> dict[str, Any]:
    p = config.shared_paths()["state"]
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"symbols": {}, "updated_at": now_iso()}


def _save_observer(obs: dict[str, Any]) -> None:
    obs["updated_at"] = now_iso()
    atomic_write_json(config.shared_paths()["state"], obs)


def _get_sym_state(obs: dict[str, Any], symbol: str) -> SymbolState:
    raw = (obs.get("symbols") or {}).get(symbol)
    if raw:
        return SymbolState.from_dict(raw)
    return SymbolState(symbol=symbol)


async def manage_all(gs: GuardedSession, arms: dict[str, Any]) -> None:
    try:
        books = await fetch_book_tickers(gs, manage=True)
    except Exception as exc:
        print(f"[盯倉] 報價失敗，跳過本輪捏造退出：{exc}", flush=True)
        return
    for arm_key, st in arms.items():
        # pending first (may open), then manage positions
        process_pending(st, books, now_ms=int(time.time() * 1000))
        for sym in list(st.positions.keys()):
            b = books.get(sym)
            if not b:
                continue
            manage_position(st, sym, b)
        # reset observer when all arms flat for symbol — done in scan
        save_arm(st)


async def scan_once(
    gs: GuardedSession,
    arms: dict[str, Any],
    universe: dict[str, Any],
    obs: dict[str, Any],
    *,
    cursor: int = 0,
) -> int:
    """Scan a time-bounded slice. Returns next cursor into ranked universe.

    Manage is interleaved so kline fetches cannot block the position loop.
    """
    eligible = universe.get("eligible") or []
    filters = universe.get("filters") or {}
    # prioritize high 24h change for surge candidates (still verify with klines)
    ranked = sorted(eligible, key=lambda r: float(r.get("price_change_pct_24h") or 0), reverse=True)
    if not ranked:
        _save_observer(obs)
        print(f"【掃描】{now_iso()}｜宇宙空｜新訊號 0", flush=True)
        return 0

    n = len(ranked)
    start_i = int(cursor) % n
    # Cap symbols + wall time per slice so manage stays responsive
    max_syms = 12
    deadline = time.time() + float(getattr(config, "SCAN_SLICE_WALL_SEC", 25.0))
    try:
        books = await fetch_book_tickers(gs, manage=False)
    except Exception as exc:
        print(f"[掃描] book 失敗：{exc}", flush=True)
        books = {}

    signals_emitted = 0
    scanned = 0
    i = start_i
    while scanned < max_syms and time.time() < deadline:
        # manage priority before each symbol fetch
        await manage_all(gs, arms)
        if time.time() >= deadline:
            break
        row = ranked[i]
        sym = row["symbol"]
        i = (i + 1) % n
        scanned += 1
        book = books.get(sym)
        if book and float(book.get("spread") or 999) > config.MAX_SPREAD:
            continue
        try:
            bars = await asyncio.wait_for(
                fetch_completed_1m_bars(gs, sym),
                timeout=float(getattr(config, "SCAN_KLINE_WAIT_SEC", 8.0)),
            )
        except Exception:
            continue
        st_sym = _get_sym_state(obs, sym)
        # if any arm still has position/pending on symbol, keep IN_TRADE lock
        busy = any(
            (sym in a.positions) or (sym in a.pending_limit_entries) for a in arms.values()
        )
        if busy:
            st_sym.phase = "IN_TRADE"
            obs.setdefault("symbols", {})[sym] = st_sym.to_dict()
            continue
        if st_sym.phase == "IN_TRADE" and not busy:
            st_sym = reset_after_trade_cycle(st_sym)

        st_sym, signal = step_state(st_sym, bars)
        obs.setdefault("symbols", {})[sym] = st_sym.to_dict()
        if not signal:
            continue
        # spread check at signal time
        if not book or float(book.get("spread") or 999) > config.MAX_SPREAD:
            append_jsonl(
                config.shared_paths()["scan_log"],
                {"event": "SIGNAL_BLOCKED_SPREAD", "signal": signal, "at": now_iso()},
            )
            continue
        append_jsonl(config.shared_paths()["signals"], {"event": "SIGNAL", **signal})
        signals_emitted += 1
        for arm_key, arm_st in arms.items():
            # one cycle per symbol per arm
            if sym in arm_st.positions or sym in arm_st.pending_limit_entries:
                continue
            filt = filters.get(sym) or {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0, "tickSize": 0.0001}
            r = queue_short_limit(arm_st, signal=signal, filters=filt)
            append_jsonl(
                config.shared_paths()["scan_log"],
                {"event": "QUEUE_RESULT", "arm": arm_key, "symbol": sym, "result": r, "at": now_iso()},
            )
        st_sym.phase = "IN_TRADE"
        obs["symbols"][sym] = st_sym.to_dict()

    _save_observer(obs)
    print(
        f"【掃描】{now_iso()}｜切片 {scanned}/{n}｜cursor {start_i}->{i}｜新訊號 {signals_emitted}",
        flush=True,
    )
    return i


def _print_wallet(arms: dict[str, Any]) -> None:
    print(f"[錢包] {now_iso()}", flush=True)
    for k, st in arms.items():
        label = config.ARMS[k]["label"]
        npos = len(st.positions)
        npend = len(st.pending_limit_entries)
        eq = st.initial_capital_u + st.realized_net_u
        print(
            f"  · {label}｜已實現 {st.realized_net_u:+.2f}｜現金 {st.cash_u:.2f}｜持倉 {npos}/{config.MAX_CONCURRENT_POSITIONS}｜掛單 {npend}",
            flush=True,
        )


async def run_paper_loop(*, hours: float = 0.0, once: bool = False) -> None:
    shared = config.shared_paths()
    ensure_dirs(shared["root"], shared["root"] / "runs")
    for arm in config.ARMS:
        ensure_dirs(config.arm_paths(arm)["root"], config.arm_paths(arm)["vault"].parent)

    arms = {k: load_arm(k) for k in config.ARMS}
    obs = _load_observer()

    timeout = aiohttp.ClientTimeout(total=float(config.HTTP_OP_BUDGET_SEC) + 5.0)
    connector = aiohttp.TCPConnector(limit=config.KLINES_API_CONCURRENCY + 4)
    start = time.time()
    universe: dict[str, Any] = {"eligible": [], "filters": {}}
    last_uni = 0.0
    scan_cursor = 0
    last_manage_heartbeat = 0.0

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        gs = GuardedSession(session)
        print("=" * 64, flush=True)
        print("啟動 A 暴漲轉弱做空 paper（僅公開行情，禁止實盤下單）", flush=True)
        print(f"版本：{config.STRATEGY_VERSION}｜三臂獨立資金簿", flush=True)
        print(f"TTL政策：{config.TTL_POLICY}", flush=True)
        print(f"硬排除：{sorted(config.HARD_EXCLUDE_SYMBOLS)}｜大型幣其餘未全排除", flush=True)
        print("=" * 64, flush=True)

        universe = await refresh_universe(gs)
        last_uni = now_ts()
        print(f"[宇宙] 合格 {universe.get('eligible_count')}｜{universe.get('large_cap_note')}", flush=True)
        _print_wallet(arms)

        if once:
            await manage_all(gs, arms)
            await scan_once(gs, arms, universe, obs, cursor=0)
            _print_wallet(arms)
            return

        while True:
            if hours > 0 and (time.time() - start) >= hours * 3600:
                print("[結束] 達到時數上限，保存退出", flush=True)
                break
            # manage first
            await manage_all(gs, arms)
            now = now_ts()
            if now - last_manage_heartbeat >= 60.0:
                print(f"[盯倉] {now_iso()}｜三臂管理心跳（掃描不阻塞）", flush=True)
                last_manage_heartbeat = now
            # universe refresh
            if now_ts() - last_uni >= config.UNIVERSE_REFRESH_MINUTES * 60:
                try:
                    universe = await refresh_universe(gs)
                    last_uni = now_ts()
                except Exception as exc:
                    print(f"[宇宙] 刷新失敗：{exc}", flush=True)
            # scan near minute boundary (bounded slice; cursor rotates universe)
            sec = int(time.time()) % 60
            if sec >= config.SCAN_SECOND and sec < config.SCAN_SECOND + 12:
                scan_cursor = await scan_once(gs, arms, universe, obs, cursor=scan_cursor)
                _print_wallet(arms)
                await asyncio.sleep(3)
            else:
                await asyncio.sleep(config.MANAGE_DUE_SEC)
