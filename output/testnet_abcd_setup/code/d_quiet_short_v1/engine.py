"""D paper loop: manage-first quiet-short scanner (15m)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import aiohttp

from . import config
from .market import (
    GuardedSession,
    fetch_book_tickers,
    fetch_completed_15m_bars,
    fetch_depth_analysis,
    refresh_universe,
    walk_book,
)
from . import console_zh
from .portfolio import (
    check_exit_triggers,
    close_position,
    cover_exit_price,
    load_account,
    process_pending,
    queue_short_limit,
    save_account,
    sort_signals,
    sync_settlement_pending,
    update_position_marks,
)
from .reporting import write_pending_confirmations, write_review_stub
from .signals import (
    SymbolState,
    after_exit,
    mark_in_trade,
    quiet_still_valid,
    try_emit_signal,
)
from .util import PersistError, append_jsonl, atomic_write_json, ensure_dirs, now_iso, now_ts

_LAST_STATUS_PRINT_TS = 0.0
_STATUS_PRINT_INTERVAL_SEC = 60.0


def _load_observer() -> dict[str, Any]:
    p = config.paths()["state"]
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"symbols": {}, "updated_at": now_iso()}


def _save_observer(obs: dict[str, Any]) -> None:
    obs["updated_at"] = now_iso()
    atomic_write_json(config.paths()["state"], obs)


def _get_sym(obs: dict[str, Any], symbol: str) -> SymbolState:
    raw = (obs.get("symbols") or {}).get(symbol)
    if raw:
        return SymbolState.from_dict(raw)
    return SymbolState(symbol=symbol)


def _sync_settlement_on_manage(st, *, settlement_fetch_fn: Any = None) -> dict[str, Any] | None:
    """Formal manage hook: sync pending even when positions empty. Paper skips exchange."""
    pending = getattr(st, "settlement_pending", None) or {}
    if not pending:
        return None
    if settlement_fetch_fn is not None:
        return sync_settlement_pending(st, fetch_user_trades_fn=settlement_fetch_fn)
    try:
        from . import testnet_hooks

        if not testnet_hooks.enabled():
            return {"ok": True, "skipped": True, "reason": "paper_or_orders_disabled_no_exchange"}
        return sync_settlement_pending(st)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "note": "settlement_sync_manage_soft_fail"}


async def manage_all(
    gs: GuardedSession,
    st,
    obs: dict[str, Any],
    *,
    bars_cache: Optional[dict[str, Any]] = None,
    settlement_fetch_fn: Any = None,
) -> None:
    try:
        books = await fetch_book_tickers(gs, manage=True)
    except Exception as exc:
        print(f"[盯倉] 報價失敗，跳過本輪捏造退出：{exc}", flush=True)
        append_jsonl(
            config.paths()["events"],
            {"event": "MANAGE_QUOTE_FAIL", "error": str(exc), "at": now_iso(), "no_fabricated_exit": True},
        )
        _sync_settlement_on_manage(st, settlement_fetch_fn=settlement_fetch_fn)
        save_account(st)
        return

    halted = bool(
        getattr(st, "persist_halt_new_entries", False) or getattr(st, "persist_recovery_required", False)
    )

    # eligibility fails for pendings (quiet broken / liquidity)
    elig_fail: dict[str, str] = {}
    for sym in list(st.pending_limit_entries.keys()):
        book = books.get(sym)
        if not book:
            continue
        if float(book.get("spread") or 999) > config.MAX_SPREAD:
            elig_fail[sym] = "SPREAD_FAIL"
            continue
        # dual near-touch depth — blocks NEW entry remainder / pending
        if config.depth_ready():
            depth = await fetch_depth_analysis(gs, sym)
            if depth is None or not depth.get("entry_depth_ok"):
                elig_fail[sym] = "DEPTH_FAIL_DUAL_NEAR_TOUCH"
                continue
        if bars_cache and sym in bars_cache:
            ok, reason = quiet_still_valid(bars_cache[sym])
            if not ok:
                elig_fail[sym] = f"QUIET_FAIL:{reason}"

    process_pending(st, books, now_ms=int(time.time() * 1000), eligibility_fail=elig_fail)

    # 現場狀態：掛單價位／未實現（與 B／C 同級可見性；不改交易邏輯）
    global _LAST_STATUS_PRINT_TS
    now_s = now_ts()
    if (st.positions or st.pending_limit_entries or halted) and (
        now_s - _LAST_STATUS_PRINT_TS >= _STATUS_PRINT_INTERVAL_SEC
    ):
        prefix = "[盯倉-持久化中斷]" if halted else "[盯倉]"
        console_zh.print_wallet(st, books=books, prefix=prefix)
        if halted:
            print(
                f"{prefix} {now_iso()}｜禁止新增掛單／開倉；"
                f"持倉模擬出場暫停（無可靠記帳）；非正常風控狀態",
                flush=True,
            )
        _LAST_STATUS_PRINT_TS = now_s

    # Native quote / VOL input record (D-local) for later forensic rebuild
    for sym in list(st.positions.keys()) + list(st.pending_limit_entries.keys()):
        b = books.get(sym)
        if not b:
            continue
        pos = st.positions.get(sym) or {}
        append_jsonl(
            config.paths()["quote_vol"],
            {
                "at": now_iso(),
                "symbol": sym,
                "bid": b.get("bid"),
                "ask": b.get("ask"),
                "mid": b.get("mid"),
                "fetched_at": b.get("fetched_at"),
                "roll_hi": pos.get("roll_hi"),
                "roll_lo": pos.get("roll_lo"),
                "roll_n": len(pos.get("roll_samples") or []),
                "T": pos.get("T"),
                "price_kind": "bookTicker_mid_bid_ask",
                "persist_halted": halted,
            },
        )

    for sym in list(st.positions.keys()):
        b = books.get(sym)
        if not b:
            continue
        # Marks: memory always; durable save only when not halted
        try:
            update_position_marks(st, sym, b, persist=not halted)
        except PersistError:
            halted = True
            append_jsonl(
                config.paths()["events"],
                {
                    "event": "MANAGEMENT_INTERRUPTED_PERSIST",
                    "symbol": sym,
                    "phase": "update_position_marks",
                    "at": now_iso(),
                    "note": "mark save failed; not normal risk control",
                },
            )
            raise
        triggers = check_exit_triggers(st, sym, b)
        if not triggers:
            continue
        if halted or getattr(st, "persist_halt_new_entries", False):
            # Do NOT simulate exit without reliable ledger — alert only
            append_jsonl(
                config.paths()["events"],
                {
                    "event": "MANAGEMENT_INTERRUPTED_NO_RELIABLE_EXIT",
                    "symbol": sym,
                    "triggers": triggers,
                    "at": now_iso(),
                    "no_fabricated_exit": True,
                    "note": "persist unreliable; exit signal observed but simulated close suppressed",
                },
            )
            print(
                f"[管理中斷] {now_iso()}｜{sym} 見出場觸發 {triggers} 但持久化不可靠，"
                f"不執行模擬平倉／不成交記帳｜非正常盯倉",
                flush=True,
            )
            continue
        # Exit NEVER refused for depth: walk available asks; disclose shortfall.
        qty = float(st.positions[sym]["qty_remaining"])
        ask = float(b.get("ask") or 0)
        if ask <= 0:
            append_jsonl(
                config.paths()["events"],
                {
                    "event": "EXIT_BLOCKED_NO_VALID_QUOTE",
                    "symbol": sym,
                    "triggers": triggers,
                    "at": now_iso(),
                    "no_fabricated_exit": True,
                },
            )
            continue
        depth_meta: dict[str, Any] = {"exit_depth_policy": "never_block_exit"}
        exit_px = cover_exit_price(ask)
        raw_walk = await _walk_asks_for_qty(gs, sym, qty)
        if raw_walk and raw_walk.get("vwap"):
            exit_px = float(raw_walk["vwap"])
            depth_meta["walk"] = raw_walk
            depth_meta["depth_shortfall"] = bool(raw_walk.get("shortfall"))
            if raw_walk.get("shortfall"):
                # remainder marked at last walked VWAP already; disclose
                depth_meta["shortfall_note"] = (
                    "ask book insufficient for full qty; VWAP uses available depth only; "
                    "shortfall disclosed — not a refused exit"
                )
        else:
            depth_meta["depth_fallback"] = "ask_plus_slip"
        reason = "+".join(triggers) if len(triggers) > 1 else triggers[0]
        try:
            closed = close_position(
                st, sym, exit_px=exit_px, reason=reason, triggers=triggers, depth_meta=depth_meta
            )
        except PersistError:
            append_jsonl(
                config.paths()["events"],
                {
                    "event": "MANAGEMENT_INTERRUPTED_PERSIST",
                    "symbol": sym,
                    "phase": "close_position",
                    "triggers": triggers,
                    "at": now_iso(),
                    "note": "close path persist failed; halt new entries; reconcile required",
                },
            )
            print(
                f"[管理中斷] {now_iso()}｜{sym} 模擬平倉記帳失敗｜非正常風控｜需對帳",
                flush=True,
            )
            raise
        if closed and closed.get("event") == "CLOSE":
            require_rebuild = "VOL_SPIKE" in reason
            sst = _get_sym(obs, sym)
            sst = after_exit(
                sst,
                reason=reason,
                exit_ts_ms=int(time.time() * 1000),
                require_rebuild=require_rebuild,
            )
            obs.setdefault("symbols", {})[sym] = sst.to_dict()
    # Pending settlement sync even when positions empty
    _sync_settlement_on_manage(st, settlement_fetch_fn=settlement_fetch_fn)
    if not getattr(st, "persist_halt_new_entries", False):
        save_account(st)
    else:
        append_jsonl(
            config.paths()["events"],
            {
                "event": "MANAGE_SKIP_ACCOUNT_SAVE_WHILE_HALTED",
                "at": now_iso(),
                "note": "quotes/MTM memory-only; no durable claim",
            },
        )
    _save_observer(obs)


async def _walk_asks_for_qty(gs: GuardedSession, symbol: str, qty: float) -> Optional[dict[str, Any]]:
    try:
        data = await gs.get_json(
            "/fapi/v1/depth",
            params={"symbol": symbol, "limit": config.DEPTH_API_LIMIT},
            purpose="depth_exit",
            op_budget_sec=8.0,
            max_attempts=2,
            request_timeout_sec=5.0,
        )
    except Exception:
        return None
    from .market import _parse_levels

    asks = _parse_levels((data or {}).get("asks"))
    return walk_book(asks, target_qty=qty)


async def scan_once(
    gs: GuardedSession,
    st,
    universe: dict[str, Any],
    obs: dict[str, Any],
    *,
    cursor: int = 0,
    allow_fills: bool = False,
) -> int:
    eligible = universe.get("eligible") or []
    filters = universe.get("filters") or {}
    ranked = sorted(eligible, key=lambda r: str(r.get("symbol") or ""))
    if not ranked:
        print(f"【掃描】{now_iso()}｜宇宙空", flush=True)
        return 0
    n = len(ranked)
    start_i = int(cursor) % n
    max_syms = 10
    deadline = time.time() + float(config.SCAN_SLICE_WALL_SEC)
    try:
        books = await fetch_book_tickers(gs, manage=False)
    except Exception as exc:
        print(f"[掃描] book 失敗：{exc}", flush=True)
        books = {}

    signal_candidates: list[dict[str, Any]] = []
    scanned = 0
    i = start_i
    bars_cache: dict[str, Any] = {}

    while scanned < max_syms and time.time() < deadline:
        await manage_all(gs, st, obs, bars_cache=bars_cache)
        if time.time() >= deadline:
            break
        row = ranked[i]
        sym = row["symbol"]
        i = (i + 1) % n
        scanned += 1
        book = books.get(sym)
        if not book:
            continue
        if float(book.get("spread") or 999) > config.MAX_SPREAD:
            continue
        age = now_ts() - float(book.get("fetched_at") or 0)
        if age > config.QUOTE_MAX_AGE_SEC:
            continue
        try:
            bars = await asyncio.wait_for(
                fetch_completed_15m_bars(gs, sym),
                timeout=float(config.SCAN_KLINE_WAIT_SEC),
            )
        except Exception:
            continue
        bars_cache[sym] = bars

        # replay archive: D uses 15m (not 1m) for signals — must record actual TF
        try:
            from common.replay_archive.hooks import bars_from_raw_klines
            from common.replay_archive.strategy_bridge import record_decision_safe, record_scan_bars

            bar_dicts = bars_from_raw_klines(bars)
            for b in bar_dicts:
                b["is_closed"] = True
                b["timeframe"] = str(config.KLINE_INTERVAL)
            ingest = record_scan_bars(
                symbol=sym,
                timeframe=str(config.KLINE_INTERVAL),
                bars=bar_dicts,
                requested_bar_count=int(config.KLINES_FETCH_LIMIT),
            )
            used_n = min(int(config.QUIET_BARS), len(bars))
            record_decision_safe(
                {
                    "decision_type": "scan",
                    "symbol": sym,
                    "direction": "SHORT",
                    "requested_bar_count": int(config.KLINES_FETCH_LIMIT),
                    "received_bar_count": int(ingest.get("received_bar_count") or len(bars)),
                    "used_bar_count": used_n,
                    "bar_refs": [
                        {
                            "open_time": int(b.open_time),
                            "close_time": int(b.close_time),
                            "timeframe": str(config.KLINE_INTERVAL),
                        }
                        for b in bars[-used_n:]
                    ],
                    "first_open_time": ingest.get("first_open_time"),
                    "last_open_time": ingest.get("last_open_time"),
                    "uses_unclosed_bar": False,
                    "timeframe": str(config.KLINE_INTERVAL),
                    "non_1m_dependency": True,
                    "final_action": "EVAL",
                }
            )
        except Exception:
            pass

        sst = _get_sym(obs, sym)
        busy = (sym in st.positions) or (sym in st.pending_limit_entries)
        if busy:
            sst.phase = "IN_TRADE" if sym in st.positions else "ARMED_SIGNAL"
            obs.setdefault("symbols", {})[sym] = sst.to_dict()
            continue

        sst, signal = try_emit_signal(
            sst,
            bars,
            P=float(book["mid"]),
            price_source=str(book.get("source") or "bookTicker"),
            price_ts=float(book.get("fetched_at") or now_ts()),
            spread=float(book.get("spread") or 999),
        )
        obs.setdefault("symbols", {})[sym] = sst.to_dict()
        if not signal:
            continue
        try:
            from common.replay_archive.strategy_bridge import record_decision_safe

            record_decision_safe(
                {
                    "decision_type": "entry",
                    "symbol": sym,
                    "direction": "SHORT",
                    "requested_bar_count": int(config.KLINES_FETCH_LIMIT),
                    "received_bar_count": len(bars),
                    "used_bar_count": min(int(config.QUIET_BARS), len(bars)),
                    "bar_refs": [
                        {
                            "open_time": int(b.open_time),
                            "close_time": int(b.close_time),
                            "timeframe": str(config.KLINE_INTERVAL),
                        }
                        for b in bars[-int(config.QUIET_BARS) :]
                    ],
                    "signal_id": signal.get("signal_id"),
                    "final_action": "SIGNAL",
                    "uses_unclosed_bar": False,
                    "timeframe": str(config.KLINE_INTERVAL),
                    "non_1m_dependency": True,
                    "also_depends_on": ["bookTicker", "depth_optional"],
                }
            )
        except Exception:
            pass
        # dual near-touch depth + 3000U walk diagnostic
        if config.depth_ready():
            depth = await fetch_depth_analysis(gs, sym)
            signal["depth"] = depth
            if depth is None or not depth.get("entry_depth_ok"):
                append_jsonl(
                    config.paths()["scan_log"],
                    {
                        "event": "SIGNAL_BLOCKED_DEPTH",
                        "signal_id": signal["signal_id"],
                        "depth": depth,
                        "at": now_iso(),
                    },
                )
                continue
        else:
            signal["depth"] = None
            signal["depth_note"] = "DEPTH_GATE_UNCONFIRMED"

        append_jsonl(config.paths()["signals"], {"event": "SIGNAL", **signal})
        signal_candidates.append(signal)

    # allocate slots with fixed ranking
    queued = 0
    blocked = 0
    for signal in sort_signals(signal_candidates):
        sym = signal["symbol"]
        if sym in st.positions or sym in st.pending_limit_entries:
            continue
        if st.slot_count() >= config.MAX_CONCURRENT_SLOTS:
            blocked += 1
            append_jsonl(
                config.paths()["scan_log"],
                {"event": "SIGNAL_BLOCKED_SLOT_CAP", "signal_id": signal["signal_id"], "at": now_iso()},
            )
            continue
        filt = filters.get(sym) or {
            "stepSize": 0.001,
            "minQty": 0.001,
            "minNotional": 5.0,
            "tickSize": 0.0001,
        }
        r = queue_short_limit(st, signal=signal, filters=filt, allow_fills=allow_fills)
        append_jsonl(
            config.paths()["scan_log"],
            {"event": "QUEUE_RESULT", "symbol": sym, "result": r, "at": now_iso()},
        )
        if r.get("ok"):
            queued += 1
            pend = r.get("pending") or {}
            if pend:
                console_zh.print_limit_queued(pend)
            sst = _get_sym(obs, sym)
            sst = mark_in_trade(sst) if False else sst
            sst.phase = "ARMED_SIGNAL"
            obs.setdefault("symbols", {})[sym] = sst.to_dict()
        else:
            blocked += 1

    _save_observer(obs)
    mode = "FILLS" if allow_fills and config.paper_fills_allowed() else "SCAN_ONLY"
    print(
        f"【掃描】{now_iso()}｜模式 {mode}｜切片 {scanned}/{n}｜cursor {start_i}->{i}｜"
        f"訊號 {len(signal_candidates)}｜掛單成功 {queued}｜阻擋 {blocked}",
        flush=True,
    )
    return i


def _print_wallet(st, books: Optional[dict] = None) -> None:
    console_zh.print_wallet(st, books=books)


async def run_paper_loop(*, hours: float = 0.0, once: bool = False, allow_fills: bool = False) -> None:
    paths = config.paths()
    ensure_dirs(paths["root"], paths["settlement"])
    write_pending_confirmations()
    write_review_stub()

    st = load_account()
    obs = _load_observer()
    timeout = aiohttp.ClientTimeout(total=float(config.HTTP_OP_BUDGET_SEC) + 5.0)
    connector = aiohttp.TCPConnector(limit=config.KLINES_API_CONCURRENCY + 4)
    start = time.time()
    universe: dict[str, Any] = {"eligible": [], "filters": {}}
    last_uni = 0.0
    scan_cursor = 0
    last_hb = 0.0

    fills_on = bool(allow_fills and config.paper_fills_allowed())
    if allow_fills and not config.paper_fills_allowed():
        print("警告：要求成交但資金／深度口徑未確認 → 強制僅掃描", flush=True)
        fills_on = False

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        gs = GuardedSession(session)
        print("=" * 64, flush=True)
        print("啟動 D 低波動小幣做空 paper（僅公開行情，禁止實盤下單）", flush=True)
        print(f"版本：{config.STRATEGY_VERSION}", flush=True)
        print(f"模式：{'PAPER_FILLS' if fills_on else 'SCAN_ONLY'}", flush=True)
        print(f"TTL政策：{config.TTL_POLICY}", flush=True)
        print(f"硬排除：{sorted(config.HARD_EXCLUDE_SYMBOLS)}｜{config.LARGE_CAP_NOTE}", flush=True)
        print(f"待確認：{config.pending_confirmations()}", flush=True)
        print("=" * 64, flush=True)

        universe = await refresh_universe(gs)
        last_uni = now_ts()
        print(f"[宇宙] 合格 {universe.get('eligible_count')}｜depth_confirmed={config.depth_ready()}", flush=True)
        _print_wallet(st)

        if once:
            await manage_all(gs, st, obs)
            await scan_once(gs, st, universe, obs, cursor=0, allow_fills=fills_on)
            _print_wallet(st)
            return

        while True:
            if hours > 0 and (time.time() - start) >= hours * 3600:
                print("[結束] 達到時數上限", flush=True)
                break
            if getattr(st, "persist_halt_new_entries", False):
                fills_on = False
                print(
                    f"[持久化故障] {now_iso()}｜已停止新增掛單／開倉；"
                    f"error={getattr(st, 'persist_last_error', '')}｜需先對帳再恢復",
                    flush=True,
                )
            try:
                await manage_all(gs, st, obs)
            except PersistError as exc:
                fills_on = False
                print(
                    f"[持久化故障] manage 寫入失敗，停止新增成交：{exc}",
                    flush=True,
                )
                # Keep looping for visibility but do not scan/queue new entries
                await asyncio.sleep(config.MANAGE_DUE_SEC)
                continue
            now = now_ts()
            if now - last_hb >= 60.0:
                print(f"[盯倉] {now_iso()}｜管理心跳（掃描不阻塞）", flush=True)
                last_hb = now
            if now_ts() - last_uni >= config.UNIVERSE_REFRESH_MINUTES * 60:
                try:
                    universe = await refresh_universe(gs)
                    last_uni = now_ts()
                except Exception as exc:
                    print(f"[宇宙] 刷新失敗：{exc}", flush=True)
            sec = int(time.time()) % 60
            # 15m boundary ~ :00; scan window early in minute after close
            if sec >= config.SCAN_SECOND and sec < config.SCAN_SECOND + 20:
                try:
                    scan_cursor = await scan_once(
                        gs, st, universe, obs, cursor=scan_cursor, allow_fills=fills_on
                    )
                except PersistError as exc:
                    fills_on = False
                    print(f"[持久化故障] scan 寫入失敗，停止新增成交：{exc}", flush=True)
                _print_wallet(st)
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(config.MANAGE_DUE_SEC)
