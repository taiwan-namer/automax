"""H v1 paper loop: Binance public rank + aggTrade feed → signals → paper limits.

Paper only invents nothing from future/closed current-bar klines.
Trade feed uses public aggTrade (no API key, no orders).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import aiohttp

from . import config
from .market import GuardedSession, fetch_book_tickers, refresh_universe
from .metrics import gross_pnl_pct
from .metrics import surge_ok
from .portfolio import (
    get_surge_store,
    load_account,
    maybe_queue_add,
    on_drop_from_top15,
    on_reenter_top5,
    process_pending_entries,
    process_pending_exits,
    queue_entry,
    save_account,
    slot_count,
    sync_surge_store,
    update_trail,
)
from .entry_gate import get_entry_gate_store, reset_entry_gate_store_for_tests  # noqa: F401
from .signals import evaluate_entry, mark_candidate_consumed
from .trade_feed import BinancePublicAggTradeFeed
from .universe import rank_is_fresh, update_tracking
from .util import append_jsonl, atomic_write_json, ensure_dirs, now_iso, now_ts


def _mark_px(pos: dict[str, Any], book: Optional[dict[str, Any]]) -> Optional[float]:
    if not book:
        return None
    try:
        bid = float(book["bid"])
        ask = float(book["ask"])
    except Exception:
        return None
    if bid <= 0 or ask <= 0:
        return None
    # conservative mark: LONG use bid, SHORT use ask
    if str(pos.get("side") or "").upper() == "LONG":
        return bid
    return ask


def _unrealized_u(pos: dict[str, Any], mark: float) -> float:
    side = str(pos.get("side") or "").upper()
    avg = float(pos.get("avg_entry") or 0)
    qty = float(pos.get("qty") or 0)
    if avg <= 0 or qty <= 0 or mark <= 0:
        return 0.0
    if side == "LONG":
        return (mark - avg) * qty
    return (avg - mark) * qty


def _side_zh(side: Any) -> str:
    s = str(side or "").upper()
    if s == "LONG":
        return "做多"
    if s == "SHORT":
        return "做空"
    return s or "—"


def _print_wallet(
    st: Any,
    books: Optional[dict[str, Any]] = None,
    *,
    obs: Optional[dict[str, Any]] = None,
    feed: Optional[BinancePublicAggTradeFeed] = None,
) -> None:
    """E-style Chinese wallet line + unrealized / open legs."""
    books = books or {}
    unreal = 0.0
    margin_locked = 0.0
    for sym, pos in (st.positions or {}).items():
        margin_locked += float(pos.get("margin_u") or 0)
        m = _mark_px(pos, books.get(sym))
        if m is not None:
            unreal += _unrealized_u(pos, m)
    cash = float(st.cash_u)
    realized = float(st.realized_net_u)
    initial = float(st.initial_capital_u or config.INITIAL_CAPITAL_USDT)
    equity = cash + margin_locked + unreal
    n_pos = len(st.positions or {})
    n_long = sum(
        1 for p in (st.positions or {}).values() if str(p.get("side") or "").upper() == "LONG"
    )
    n_short = sum(
        1 for p in (st.positions or {}).values() if str(p.get("side") or "").upper() == "SHORT"
    )
    n_pend = len(st.pending_entries or {})
    n_exit = len(st.pending_exits or {})
    slots = slot_count(st)
    tracked_n = len((obs or {}).get("tracked") or {})
    feed_note = ""
    if feed is not None:
        feed_note = f"｜串流={'連線' if feed.connected else '斷線'} msgs={feed.messages}"

    print(f"[錢包] {now_iso()}", flush=True)
    print(
        f"  · H_SURGE｜本金 {initial:.2f}｜現金 {cash:.2f}｜"
        f"已實現 {realized:+.2f}｜未實現 {unreal:+.2f}｜"
        f"權益約 {equity:.2f}｜保證金占用 {margin_locked:.2f}",
        flush=True,
    )
    print(
        f"  · 持倉 {n_pos}（做多{n_long}／做空{n_short}）｜槽位 {slots}/{config.MAX_SLOTS}｜"
        f"進場掛單 {n_pend}｜退出中 {n_exit}｜追蹤 {tracked_n}"
        f"{feed_note}",
        flush=True,
    )
    tracked_map = (obs or {}).get("tracked") or {}
    if tracked_map:
        # show enter-eligible vs keep-only so Top10 members are visible
        enter_syms = sorted(
            s for s, m in tracked_map.items() if m.get("signal_eligible")
        )
        keep_only = sorted(
            s for s, m in tracked_map.items() if not m.get("signal_eligible")
        )
        print(
            f"  · 訊號追蹤(Top{config.TOP_ENTER}) {enter_syms}",
            flush=True,
        )
        if keep_only:
            print(
                f"  · 僅維持(Top{config.TOP_KEEP}內) {keep_only}",
                flush=True,
            )
    if st.positions:
        for sym, pos in sorted(st.positions.items()):
            m = _mark_px(pos, books.get(sym))
            u = _unrealized_u(pos, m) if m is not None else None
            g = gross_pnl_pct(str(pos.get("side")), float(pos.get("avg_entry") or 0), float(m or 0))
            g_s = f"{g*100:+.2f}%" if g is not None and m is not None else "n/a"
            u_s = f"{u:+.2f}" if u is not None else "n/a"
            mark_s = f"{m:.6g}" if m is not None else "—"
            print(
                f"    - {sym} {_side_zh(pos.get('side'))} qty={float(pos.get('qty') or 0):g} "
                f"均價={float(pos.get('avg_entry') or 0):.6g} 標記={mark_s} "
                f"未實現={u_s} ({g_s}) "
                f"波次={int(pos.get('legs_filled') or 0)}/{config.MAX_LEGS_PER_SYMBOL} "
                f"追蹤停={float(pos.get('trail_stop') or 0):.6g}"
                f"{' PROFIT' if pos.get('profit_trailing') else ''}",
                flush=True,
            )
    if st.pending_entries:
        for sym, pe in sorted(st.pending_entries.items()):
            ot = str(pe.get("order_type") or "LIMIT").upper()
            label = "市價待確認" if ot == "MARKET" else "掛單"
            print(
                f"    · {label} {sym} {_side_zh(pe.get('side'))}@"
                f"{float(pe.get('limit_px') or 0):.6g} "
                f"qty={float(pe.get('qty') or 0):g} leg={pe.get('leg_index')}",
                flush=True,
            )

_GUARDS: dict[str, Any] = {}


def _guard_once(st: Any) -> None:
    try:
        from . import testnet_hooks

        if not testnet_hooks.enabled():
            return
        from common.exchange_guard import ExchangeGuard
        from common.testnet_bridge import get_adapter
    except Exception:  # noqa: BLE001
        return

    tracked = set(st.positions) | {
        s for s, pe in (st.pending_entries or {}).items() if int(pe.get("leg_index") or 1) == 1
    }

    def stop_for(sym: str, entry: float) -> float:
        pd = st.positions.get(sym)
        if pd and float(pd.get("trail_stop") or 0) > 0:
            return float(pd["trail_stop"])
        side = str((pd or {}).get("side") or "LONG").upper()
        if side == "SHORT":
            return float(entry) * (1.0 + float(config.TRAIL_PCT))
        return float(entry) * (1.0 - float(config.TRAIL_PCT))

    for side in ("LONG", "SHORT"):
        key = f"H_{side}"
        g = _GUARDS.get(key)
        if g is None:
            g = _GUARDS[key] = ExchangeGuard(
                strategy="H", position_side=side, label=f"H-{side}"
            )
        if not g.due():
            continue
        side_tracked = {
            s
            for s in tracked
            if s in st.positions and str(st.positions[s].get("side") or "").upper() == side
        } | {
            s
            for s, pe in (st.pending_entries or {}).items()
            if str(pe.get("side") or "").upper() == side
        }
        try:
            acts = g.check(get_adapter("H"), tracked=side_tracked, stop_for=stop_for)
        except Exception as exc:  # noqa: BLE001
            print(f"[守門-H-{side}] 檢查失敗：{exc}", flush=True)
            continue
        for a in acts:
            append_jsonl(
                config.shared_paths()["scan_log"],
                {"event": f"GUARD_{a['action']}", "side": side, "at": now_iso(), **a},
            )
            pd = st.positions.get(a["symbol"])
            if pd is not None and a["action"] == "NAKED_SL":
                pd["testnet_sl_client_order_id"] = a["cid"]
                pd.pop("testnet_sl_exchange_order_id", None)
                pd["testnet_sl_stop_price"] = a["stop"]
                pd["exchange_protection_active"] = True
                pd.pop("testnet_sl_place_failed", None)
    save_account(st)


def _load_observer() -> dict[str, Any]:
    p = config.shared_paths()["state"]
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {
        "tracked": {},
        "last_rank_ts": None,
        "last_rank_ok": False,
        "updated_at": now_iso(),
        "trade_feed_attached": False,
        "note": "await_trade_feed",
    }


def _save_observer(obs: dict[str, Any]) -> None:
    obs["updated_at"] = now_iso()
    atomic_write_json(config.shared_paths()["state"], obs)


async def manage_once(gs: GuardedSession, st: Any, books: Optional[dict] = None) -> None:
    _guard_once(st)
    try:
        books = books if books is not None else await fetch_book_tickers(gs, manage=True)
    except Exception as exc:
        print(f"[盯倉] 報價失敗，不捏造成交：{exc}", flush=True)
        save_account(st)
        return
    now = now_ts()
    process_pending_entries(st, books, now=now)
    process_pending_exits(st, books, now=now)
    for sym, _pos in list(st.positions.items()):
        b = books.get(sym)
        if not b:
            continue
        mid = (float(b["bid"]) + float(b["ask"])) / 2.0
        update_trail(st, sym, mid, now=now)
        if sym not in st.pending_exits and sym not in st.dropped_tracking:
            maybe_queue_add(st, sym, bid=float(b["bid"]), ask=float(b["ask"]), now=now)
    save_account(st)


async def refresh_rank_and_track(
    gs: GuardedSession,
    st: Any,
    obs: dict[str, Any],
    *,
    feed: Optional[BinancePublicAggTradeFeed] = None,
) -> dict[str, Any]:
    snap = await refresh_universe(gs)
    now = float(snap["refreshed_ts"])
    tracked_prev = dict(obs.get("tracked") or {})
    upd = update_tracking(tracked_prev, list(snap.get("ranked") or []), now_ts=now)
    dropped = upd["dropped"]
    if dropped:
        on_drop_from_top15(st, dropped, now=now)
    reenter = [
        s
        for s, meta in upd["tracked"].items()
        if meta.get("signal_eligible") and s in (st.dropped_tracking or {})
    ]
    if reenter:
        on_reenter_top5(st, reenter)
    obs["tracked"] = upd["tracked"]
    obs["last_rank_ts"] = now
    obs["last_rank_ok"] = True
    obs["top5"] = upd["top5"]
    obs["top15"] = upd["top15"]
    obs["filters"] = snap.get("filters") or obs.get("filters") or {}
    _save_observer(obs)
    append_jsonl(
        config.shared_paths()["scan_log"],
        {
            "event": "RANK_REFRESH",
            "at": now_iso(),
            "entered": upd["entered"],
            "dropped": dropped,
            "tracked_n": len(upd["tracked"]),
            "top5": upd["top5"],
        },
    )
    print(
        f"[排名] Top{config.TOP_ENTER}={upd['top5']}｜追蹤 {len(upd['tracked'])}｜"
        f"可交易 {snap.get('tradable_usdt_perp')}｜排除 {snap.get('excluded_n')}",
        flush=True,
    )
    if feed is not None:
        # Subscribe tracked + open positions (risk tape)
        syms = set(upd["tracked"]) | set(st.positions) | set(st.pending_entries)
        await feed.set_symbols(syms)
    return snap


async def evaluate_symbol_signal(
    *,
    gs: GuardedSession,
    st: Any,
    obs: dict[str, Any],
    feed: BinancePublicAggTradeFeed,
    symbol: str,
    books: dict[str, Any],
) -> None:
    """Evaluate A/B/C paths; queue paper entry if ok. Cont_arm default OFF."""
    meta = (obs.get("tracked") or {}).get(symbol) or {}
    if not meta.get("signal_eligible"):
        return
    if not rank_is_fresh(obs.get("last_rank_ts"), now_ts(), config.RANK_STALE_STOP_NEW_SEC):
        return
    if symbol in (st.dropped_tracking or {}):
        return

    tape = feed.get_tape(symbol)
    now = now_ts()
    eval_sec = int(now) - 1
    # once per complete second
    if eval_sec <= int(tape.last_eval_sec or -1):
        return
    if now - float(tape.last_eval_ts or 0) < 0.05:
        return
    tape.last_eval_ts = now
    tape.last_eval_sec = eval_sec

    bar = tape.bars.known_current(now)
    baseline = tape.bars.baseline_median(config.RVOL_LOOKBACK_BARS)
    prices60 = tape.grid.last_n_prices(config.ER_WINDOW_SEC)
    prices15 = tape.grid.last_n_prices(15)
    has_pos = symbol in st.positions
    has_exit = symbol in st.pending_exits
    has_pending = symbol in st.pending_entries
    bar_start = float(bar.start_ts) if bar else -1.0
    pos = (st.positions or {}).get(symbol) or {}
    pos_side = str(pos.get("side") or "") if has_pos else None

    gate_store = get_entry_gate_store(st.entry_gates)
    gate = gate_store.get(symbol)

    store = get_surge_store(st)
    store.expire_if_needed(symbol, now=now)

    # Arm origin surge event only when cont_arm enabled (rollback path)
    if (
        config.ENABLE_CONTINUATION_ARM
        and bar is not None
        and baseline is not None
        and float(baseline) > 0
        and not has_pos
    ):
        elapsed = now - float(bar.start_ts)
        ok_surge, surge_detail = surge_ok(
            float(bar.quote_volume),
            float(baseline),
            elapsed,
            min_elapsed=config.RVOL_MIN_ELAPSED_SEC,
            rvol_threshold=config.RVOL_THRESHOLD,
            abs_frac=config.RVOL_ABS_FRAC_OF_BASELINE,
            bar_sec=float(config.BAR_SEC),
        )
        if ok_surge:
            ev, why = store.try_create_origin(
                symbol=symbol,
                source_bar_open_time=float(bar.start_ts),
                detected_at=now,
                baseline_frozen=float(baseline),
                source_rvol=float(surge_detail.get("rvol") or 0),
                has_position=has_pos,
                has_pending_entry=has_pending,
            )
            append_jsonl(
                config.shared_paths()["scan_log"],
                {
                    "event": "SURGE_EVENT_ARM",
                    "symbol": symbol,
                    "why": why,
                    "event_id": (ev.event_id if ev else None),
                    "source_bar_open_time": float(bar.start_ts),
                    "baseline_frozen": float(baseline),
                    "source_rvol": surge_detail.get("rvol"),
                    "expires_at": (ev.expires_at if ev else None),
                    "at": now_iso(),
                },
            )
            sync_surge_store(st)

    surge_ev = store.get(symbol) if config.ENABLE_CONTINUATION_ARM else None
    filters = (obs.get("filters") or {}).get(symbol) or {
        "stepSize": 0.001,
        "minQty": 0.001,
        "minNotional": 5.0,
        "tickSize": 0.0001,
    }
    tick = float(filters.get("tickSize") or 0.0001)
    flat_confirmed = (not has_pos) and (not has_exit) and (not has_pending)

    decision = evaluate_entry(
        decision_ts=now,
        bar=bar,
        baseline_median=baseline,
        prices_60=prices60,
        prices_15=prices15,
        last_trade_ts=float(tape.last_trade_ts or 0),
        gap_unfilled=bool(getattr(tape.grid, "gap_unfilled", False)),
        has_position=has_pos,
        has_exit_pending=has_exit,
        surge_event=surge_ev,
        sparse=tape.sparse,
        gate=gate,
        tick_size=tick,
        position_side=pos_side,
        has_pending_entry=has_pending,
        flat_confirmed=flat_confirmed,
    )
    st.entry_gates = gate_store.to_dict()

    if (
        config.ENABLE_CONTINUATION_ARM
        and decision.reason in ("TRADE_STALE", "TRADE_STREAM_GAP")
        and surge_ev
        and surge_ev.is_active()
        and surge_ev.state != "RESERVED"
    ):
        store.invalidate(symbol, reason=decision.reason, now=now)
        sync_surge_store(st)

    row = {
        "event": "SIGNAL_DIAG",
        "symbol": symbol,
        "ok": decision.ok,
        "side": decision.side,
        "tags": decision.tags,
        "reason": decision.reason,
        "entry_path": decision.entry_path,
        "event_id": decision.event_id,
        "candidate_id": decision.candidate_id,
        "paths": decision.paths,
        "metrics": decision.metrics,
        "rank_eligible": bool(meta.get("signal_eligible")),
        "strategy_version": config.STRATEGY_VERSION,
        "config_hash": config.config_hash(),
        "at": now_iso(),
        "feed_msgs": feed.messages,
        "tape_trades": tape.trade_count,
    }
    # Disk: signals = actionable candidates; rejects = full per-sec diag (throttle non-notable)
    if decision.ok:
        append_jsonl(config.shared_paths()["signals"], {**row, "event": "SIGNAL_CANDIDATE"})
    notable = decision.ok or decision.reason in (
        "DIRECTION_CONFLICT",
        "CHOP_BLOCK",
        "COOLDOWN_AFTER_EXIT",
        "FIRST_ENTRY_RATE_LIMIT_180S",
        "OPPOSITE_POSITION",
        "ALREADY_IN_POSITION",
        "CANDIDATE_MUST_CLEAR_BEFORE_REARM",
        "TRADE_STALE",
        "DATA_INSUFFICIENT",
    ) or any(
        (decision.paths.get(k) or {}).get("ok")
        for k in ("SURGE", "CONTINUATION", "REVERSAL")
    )
    if notable:
        append_jsonl(config.shared_paths()["rejects"], row)

    if not decision.ok:
        save_account(st)
        return

    if has_pos:
        # same-side: recorded only; opposite already blocked in signals
        save_account(st)
        return

    book = books.get(symbol)
    if not book:
        try:
            books.update(await fetch_book_tickers(gs, manage=True))
            book = books.get(symbol)
        except Exception:
            book = None
    if not book:
        append_jsonl(
            config.shared_paths()["rejects"],
            {"event": "SIGNAL_NO_BOOK", "symbol": symbol, "at": now_iso()},
        )
        save_account(st)
        return

    gate_store.note_attempt(symbol, now)
    mark_candidate_consumed(gate, decision.candidate_id, str(decision.side))
    st.entry_gates = gate_store.to_dict()

    ok, reason, order = queue_entry(
        st,
        symbol=symbol,
        side=str(decision.side),
        bid=float(book["bid"]),
        ask=float(book["ask"]),
        filters=filters,
        signal=decision.to_dict(),
        leg_index=1,
        bar_start_ts=bar_start,
        now=now,
    )
    append_jsonl(
        config.shared_paths()["scan_log"],
        {
            "event": "ENTRY_ATTEMPT",
            "symbol": symbol,
            "side": decision.side,
            "tags": decision.tags,
            "entry_path": decision.entry_path,
            "candidate_id": decision.candidate_id,
            "event_id": decision.event_id,
            "ok": ok,
            "reason": reason,
            "order_id": (order or {}).get("order_id"),
            "order_type": (order or {}).get("order_type") or config.ENTRY_ORDER_TYPE,
            "at": now_iso(),
            "note": "attempt_not_fill",
        },
    )
    if ok:
        pos2 = (st.positions or {}).get(symbol) or {}
        fill_px = pos2.get("avg_entry") or (order or {}).get("limit_px")
        ot = str((order or {}).get("order_type") or config.ENTRY_ORDER_TYPE).upper()
        path_s = decision.entry_path or "?"
        if ot == "MARKET":
            print(
                f"[訊號] {symbol} {_side_zh(decision.side)} {path_s} tags={decision.tags} → 市價委託 "
                f"@{fill_px} qty={(order or {}).get('qty')} (attempt≠保證成交)",
                flush=True,
            )
        else:
            print(
                f"[訊號] {symbol} {_side_zh(decision.side)} {path_s} tags={decision.tags} → 已掛限價 "
                f"{(order or {}).get('limit_px')} qty={(order or {}).get('qty')}",
                flush=True,
            )
    sync_surge_store(st)
    save_account(st)


async def run_paper_loop(
    *,
    hours: float = 0.0,
    once: bool = False,
    enable_trade_feed: bool = True,
) -> None:
    paths = config.shared_paths()
    ensure_dirs(paths["root"], config.arm_paths()["root"])
    st = load_account()
    if not st.run_id:
        st.run_id = f"h_{int(time.time())}"
    store = get_surge_store(st)
    restart_notes = store.validate_after_restart(now=now_ts())
    sync_surge_store(st)
    save_account(st)
    if restart_notes:
        append_jsonl(
            config.shared_paths()["scan_log"],
            {"event": "SURGE_EVENT_RESTART_VALIDATE", "notes": restart_notes, "at": now_iso()},
        )
    obs = _load_observer()
    obs["trade_feed_attached"] = bool(enable_trade_feed)
    obs["note"] = "binance_public_aggTrade" if enable_trade_feed else "no_trade_feed"
    _save_observer(obs)

    print(
        "================================================================\n"
        f"啟動 H surge-trend paper｜版本={config.STRATEGY_VERSION}｜config_hash={config.config_hash()[:16]}\n"
        f"追蹤 Top{config.TOP_ENTER}/Keep{config.TOP_KEEP}｜槽={config.MAX_SLOTS}｜每幣{config.MAX_LEGS_PER_SYMBOL}波｜名目{config.NOTIONAL_USDT}U\n"
        f"爆量 RVOL>={config.RVOL_THRESHOLD}｜延續RVOL>={config.CONTINUATION_RVOL_THRESHOLD}"
        f"（後{config.CONTINUATION_BARS}根）｜ER>={config.ER_MIN}｜Trail {config.TRAIL_PCT:.0%}｜ProfitMark {config.PROFIT_MARK_PCT:.0%}\n"
        f"進場={config.ENTRY_ORDER_TYPE}"
        f"{'' if str(config.ENTRY_ORDER_TYPE).upper()=='MARKET' else f' TTL={config.ENTRY_TTL_SEC}s'}"
        f"｜退出限價重掛={config.EXIT_REQUOTE_SEC}s｜退出無市價 fallback\n"
        f"exchange_protection_active={st.exchange_protection_active}\n"
        f"trade_feed={'幣安公開 aggTrade（僅行情／不下單）' if enable_trade_feed else 'NONE'}\n"
        "================================================================",
        flush=True,
    )

    timeout = aiohttp.ClientTimeout(total=None)
    start = time.time()
    last_rank = 0.0
    last_manage = 0.0
    last_signal_scan = 0.0
    last_wallet = 0.0
    books: dict[str, Any] = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        gs = GuardedSession(session)
        feed: Optional[BinancePublicAggTradeFeed] = None
        feed_task = None
        if enable_trade_feed:
            feed = BinancePublicAggTradeFeed(session=session)
            feed_task = feed.start()

        try:
            try:
                await refresh_rank_and_track(gs, st, obs, feed=feed)
                last_rank = time.time()
            except Exception as exc:
                append_jsonl(paths["scan_log"], {"event": "RANK_FAIL", "error": str(exc), "at": now_iso()})
                print(f"[排名] 初次失敗：{exc}", flush=True)

            try:
                books = await fetch_book_tickers(gs, manage=False)
            except Exception:
                books = {}
            _print_wallet(st, books, obs=obs, feed=feed)
            last_wallet = time.time()

            while True:
                now = time.time()
                if hours > 0 and (now - start) >= hours * 3600:
                    print("[結束] hours 到期；不平殺", flush=True)
                    break
                if now - last_manage >= config.MANAGE_DUE_SEC:
                    st = load_account()
                    await manage_once(gs, st, books=books or None)
                    last_manage = now
                if now - last_rank >= config.RANK_REFRESH_SEC:
                    st = load_account()
                    try:
                        await refresh_rank_and_track(gs, st, obs, feed=feed)
                        last_rank = now
                    except Exception as exc:
                        obs["last_rank_ok"] = False
                        _save_observer(obs)
                        append_jsonl(
                            paths["scan_log"],
                            {"event": "RANK_FAIL", "error": str(exc), "at": now_iso()},
                        )
                if not rank_is_fresh(obs.get("last_rank_ts"), now_ts(), config.RANK_STALE_STOP_NEW_SEC):
                    if obs.get("rank_fresh") is not False:
                        append_jsonl(
                            paths["scan_log"],
                            {"event": "RANK_STALE_STOP_NEW", "at": now_iso()},
                        )
                    obs["rank_fresh"] = False
                else:
                    obs["rank_fresh"] = True

                # Signal scan on tracked names using live tapes
                if feed is not None and now - last_signal_scan >= 1.0:
                    st = load_account()
                    tracked = [
                        s
                        for s, m in (obs.get("tracked") or {}).items()
                        if m.get("signal_eligible")
                    ]
                    if tracked:
                        try:
                            books = await fetch_book_tickers(gs, manage=False)
                        except Exception as exc:
                            print(f"[掃描] book 失敗：{exc}", flush=True)
                        for sym in tracked:
                            try:
                                await evaluate_symbol_signal(
                                    gs=gs,
                                    st=st,
                                    obs=obs,
                                    feed=feed,
                                    symbol=sym,
                                    books=books,
                                )
                            except Exception as exc:  # noqa: BLE001
                                append_jsonl(
                                    paths["scan_log"],
                                    {
                                        "event": "SIGNAL_EVAL_ERR",
                                        "symbol": sym,
                                        "error": str(exc),
                                        "at": now_iso(),
                                    },
                                )
                        save_account(st)
                    last_signal_scan = now

                # E-style wallet heartbeat (~30s)
                if now - last_wallet >= 30.0:
                    st = load_account()
                    try:
                        books = await fetch_book_tickers(gs, manage=True)
                    except Exception as exc:
                        print(f"[錢包] 報價失敗，未實現暫以缺值：{exc}", flush=True)
                    _print_wallet(st, books, obs=obs, feed=feed)
                    last_wallet = now

                if once:
                    break
                await asyncio.sleep(0.2)
        finally:
            if feed is not None:
                await feed.stop()
            if feed_task is not None:
                feed_task.cancel()

    save_account(st)
    _save_observer(obs)
