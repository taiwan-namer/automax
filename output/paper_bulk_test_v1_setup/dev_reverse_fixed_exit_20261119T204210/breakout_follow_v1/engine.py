"""Main paper loop: universe refresh, scan, manage, review."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Optional

import aiohttp

from . import config
from .account import (
    ensure_day_rollover,
    get_position,
    load_or_create_account,
    save_account,
    write_run_manifest,
)
from .market_data import (
    GuardedSession,
    backfill_warmup_klines,
    closed_klines,
    fetch_book_tickers,
    fetch_klines_incremental,
    refresh_universe,
    wilder_atr,
)
from .manage_pass import await_scan_slice, get_due_clock, manage_due_pass
from .portfolio import (
    close_qty,
    count_reverses,
    estimate_net_unrealized,
    manage_position,
    mark_price_for_side,
    note_reverse,
    try_open,
)
from .signals import detect_breakout
from .util import append_jsonl, ensure_dirs, now_iso, now_ts
from .review import write_review
from .run_store import begin_run, get_store
from . import console_zh


def _source_tags(symbol: str, gainers: set[str], losers: set[str], vol_ok: bool) -> list[str]:
    tags = []
    if symbol in gainers:
        tags.append("TOP_GAINERS")
    if symbol in losers:
        tags.append("TOP_LOSERS")
    if vol_ok:
        tags.append("MARKET_SPIKE")
    return tags or ["MARKET_SPIKE"]


def _sig_key(symbol: str, signal_open_time: int, side: str) -> str:
    return f"{symbol}:{signal_open_time}:{side}"


def _candidate_rank_key(c: dict[str, Any]) -> tuple:
    """決策時既有評分：vol_x 降序、spread 升序、symbol 升序（固定可重現）。"""
    return (-float(c.get("vol_x") or 0), float(c.get("spread") or 0), str(c.get("symbol") or ""))


SCAN_BATCH_SIDE_OPEN_LIMIT = 1


def execute_reverse(
    st: Any,
    *,
    symbol: str,
    sig: dict[str, Any],
    book: dict[str, Any],
    source_tags: list[str],
    store: Any = None,
    allow_reopen: bool = True,
) -> dict[str, Any]:
    """
    Engine 層反手：須已有完整反向突破訊號。
    政策（bulk 已批准 20261119）：完整反向突破 → 平舊倉成功 → 重檢所有風控 →
    合格才開反向新倉。平倉失敗不開；日損鎖禁新倉仍可平；30 分最多 2 次。
    不得多空同時持有。無有效報價不得假造成交。
    allow_reopen=False：只平倉，回傳 pending_reopen 供同批排序後再開（同批同向上限）。
    """
    profile_id = getattr(config, "PROFILE_ID", config.STRATEGY_ID)
    if profile_id == "paper_bulk_test_v1":
        policy = "approved_full_breakout_reverse_reopen_20261119"
    elif profile_id == "paper_reverse_fixed_exit_v1":
        policy = "disabled_fixed_exit_no_reverse_reopen"
    else:
        policy = "legacy_engine_reverse"
    out: dict[str, Any] = {
        "closed": False,
        "opened": False,
        "events": [],
        "profile_id": profile_id,
        "reverse_policy": policy,
    }
    if not bool(getattr(config, "ENABLE_REVERSE_REOPEN", True)):
        out["block_reason"] = "REVERSE_DISABLED"
        return out
    pos = get_position(st, symbol)
    if pos is None or not sig or sig.get("side") == pos.side:
        out["block_reason"] = "NO_REVERSE"
        return out
    # 報價新鮮度：過期／缺失不得假造平倉
    fetched_at = float(book.get("fetched_at") or 0)
    if fetched_at <= 0 or (time.time() - fetched_at) > config.QUOTE_MAX_AGE_SEC:
        ev = {
            "event": "REVERSE_TRIGGERED",
            "symbol": symbol,
            "profile_id": profile_id,
            "reverse_policy": policy,
            "old_side": pos.side,
            "new_side": sig.get("side"),
            "block_reason": "STALE_OR_INVALID_QUOTE",
            "ts": now_iso(),
            "run_id": st.run_id,
            "old_trade_id": pos.trade_id,
        }
        out["events"].append(ev)
        out["block_reason"] = "STALE_OR_INVALID_QUOTE"
        if store is not None:
            store.log_decision(ev)
            store.note_quality(
                "HIGH_PRIORITY_EXIT_MGMT_RISK",
                symbol=symbol,
                detail="reverse aborted: stale/invalid quote; no fabricated fill",
                replay_precise=False,
            )
        return out
    mark = mark_price_for_side(pos.side, book)
    old_trade_id = pos.trade_id
    old_side = pos.side
    trig = {
        "event": "REVERSE_TRIGGERED",
        "symbol": symbol,
        "profile_id": profile_id,
        "reverse_policy": policy,
        "old_side": old_side,
        "new_side": sig["side"],
        "signal_open_time": sig.get("signal_open_time"),
        "source_tags": list(source_tags),
        "ts": now_iso(),
        "run_id": st.run_id,
        "old_trade_id": old_trade_id,
    }
    out["events"].append(trig)
    if store is not None:
        store.log_decision(trig)
    else:
        append_jsonl(config.DECISION_TRACE_PATH, trig)

    close_res = close_qty(
        st,
        symbol,
        qty=pos.qty_remaining,
        exit_price=mark,
        reason="REVERSE_SIGNAL",
        book_ts=fetched_at,
    )
    if not close_res.get("ok"):
        ev = {
            "event": "REVERSE_CLOSE_FAILED",
            "symbol": symbol,
            "profile_id": profile_id,
            "reverse_policy": policy,
            "block_reason": close_res.get("block_reason"),
            "ts": now_iso(),
            "run_id": st.run_id,
            "old_trade_id": old_trade_id,
            "close_ok": False,
            "open_ok": False,
        }
        out["events"].append(ev)
        out["block_reason"] = close_res.get("block_reason")
        if store is not None:
            store.log_decision(ev)
        return out
    out["closed"] = True
    # 不得同時持有：平倉後必須無該標的倉
    assert symbol not in st.positions
    ensure_day_rollover(st, st.equity_mark(0.0))
    if st.equity_mark(0.0) <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT:
        st.day_new_entries_locked = True
        save_account(st)
    rev_row = {
        "event": "REVERSE_CLOSE",
        "symbol": symbol,
        "profile_id": profile_id,
        "reverse_policy": policy,
        "old_side": old_side,
        "new_side": sig["side"],
        "ts": now_iso(),
        "run_id": st.run_id,
        "old_trade_id": old_trade_id,
        "reason": "REVERSE_SIGNAL",
        "close_ok": True,
        "fill_price": mark,
        "net_u": close_res.get("net"),
    }
    out["events"].append(rev_row)
    if store is not None:
        store.log_decision(rev_row)
    else:
        append_jsonl(config.DECISION_TRACE_PATH, rev_row)

    if count_reverses(st, symbol) >= config.MAX_REVERSES_PER_WINDOW:
        blk = {
            "event": "REVERSE_OPEN_BLOCKED",
            "symbol": symbol,
            "profile_id": profile_id,
            "reverse_policy": policy,
            "block_reason": "REVERSE_LIMIT",
            "ts": now_iso(),
            "run_id": st.run_id,
            "close_ok": True,
            "open_ok": False,
        }
        out["events"].append(blk)
        out["block_reason"] = "REVERSE_LIMIT"
        if store is not None:
            store.log_decision(blk)
        return out

    sk = _sig_key(symbol, int(sig["signal_open_time"]), sig["side"])
    if sk in st.processed_signals:
        out["block_reason"] = "DUPLICATE_SIGNAL"
        dup = {
            "event": "REVERSE_OPEN_REJECTED",
            "symbol": symbol,
            "profile_id": profile_id,
            "reverse_policy": policy,
            "block_reason": "DUPLICATE_SIGNAL",
            "ts": now_iso(),
            "run_id": st.run_id,
            "close_ok": True,
            "open_ok": False,
        }
        out["events"].append(dup)
        if store is not None:
            store.log_decision(dup)
        return out

    if not allow_reopen:
        out["pending_reopen"] = {
            "symbol": symbol,
            "side": sig["side"],
            "signal": sig,
            "source_tags": list(source_tags),
            "linked_prev": old_trade_id,
            "vol_x": float(sig.get("vol_x") or 0),
            "spread": float(sig.get("spread") or 0),
            "sig_key": sk,
            "bypass_cooldown": True,
            "is_reverse_reopen": True,
        }
        return out

    filt = (st.filters or {}).get(symbol, {})
    # 平倉後重新檢查所有既有風控（try_open 內含日損鎖／倉數／保證金／風險等）
    opened = try_open(
        st,
        symbol=symbol,
        side=sig["side"],
        signal=sig,
        source_tags=source_tags,
        filters=filt,
        linked_prev=old_trade_id,
        bypass_cooldown=True,
    )
    if opened.get("ok"):
        note_reverse(st, symbol)
        st.processed_signals.append(sk)
        save_account(st)
        out["opened"] = True
        ok_ev = {
            "event": "REVERSE_OPEN",
            "symbol": symbol,
            "profile_id": profile_id,
            "reverse_policy": policy,
            "trade_id": opened.get("trade_id"),
            "new_side": sig["side"],
            "ts": now_iso(),
            "run_id": st.run_id,
            "close_ok": True,
            "open_ok": True,
            "linked_prev_trade_id": old_trade_id,
        }
        out["events"].append(ok_ev)
        if store is not None:
            store.log_decision(ok_ev)
    else:
        rej = {
            "event": "REVERSE_OPEN_REJECTED",
            "symbol": symbol,
            "profile_id": profile_id,
            "reverse_policy": policy,
            "block_reason": opened.get("block_reason"),
            "block_reasons": opened.get("block_reasons"),
            "ts": now_iso(),
            "run_id": st.run_id,
            "close_ok": True,
            "open_ok": False,
        }
        out["events"].append(rej)
        out["block_reason"] = opened.get("block_reason")
        if store is not None:
            store.log_decision(rej)
        else:
            append_jsonl(config.DECISION_TRACE_PATH, rej)
    return out


def _book_usable(book: Optional[dict[str, Any]]) -> bool:
    if not book:
        return False
    try:
        bid = float(book["bid"])
        ask = float(book["ask"])
        fetched_at = float(book.get("fetched_at") or 0)
    except Exception:
        return False
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    if fetched_at <= 0 or (time.time() - fetched_at) > config.QUOTE_MAX_AGE_SEC:
        return False
    return True


async def scan_once(gs: GuardedSession, st, stats: dict[str, Any]) -> dict[str, Any]:
    """一輪分鐘掃描：先管理持倉，再收集候選，排序後開倉。"""
    t0 = time.time()
    scan_batch_id = f"sb:{st.run_id}:{uuid.uuid4().hex[:12]}"
    batch_opened_by_side: dict[str, dict[str, Any]] = {}
    books = await fetch_book_tickers(gs)
    eligible = [r["symbol"] for r in (st.universe or [])]
    gainers = set(st.top_gainers or [])
    losers = set(st.top_losers or [])
    # 持倉幣即使掉榜也要管
    symbols = sorted(set(eligible) | set(st.positions.keys()))
    stats.update(
        {
            "expected": len(symbols),
            "success": 0,
            "failed": 0,
            "deferred": 0,
            "candidates": 0,
            "opened": 0,
            "rejected": {},
            "scan_batch_id": scan_batch_id,
        }
    )

    # floating equity for day lock
    floating = 0.0
    for sym, pdct in list(st.positions.items()):
        b = books.get(sym)
        if not b:
            continue
        from .account import Position
        from .portfolio import estimate_net_unrealized

        pos = Position.from_dict(pdct)
        mark = mark_price_for_side(pos.side, b)
        floating += estimate_net_unrealized(pos, mark)

    equity = st.equity_mark(floating)
    ensure_day_rollover(st, equity)
    if equity <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT:
        st.day_new_entries_locked = True
        save_account(st)

    eq_row = {
        "ts": now_iso(),
        "equity": equity,
        "cash": st.cash_u,
        "realized_net_u": st.realized_net_u,
        "realized_fees_u": st.realized_fees_u,
        "floating_u": floating,
        "positions": len(st.positions),
        "day_locked": st.day_new_entries_locked,
        "day_start_equity_u": st.day_start_equity_u,
    }
    store = get_store()
    if store is not None:
        store.log_equity(eq_row, reason="MINUTE")
        store.mirror_account(st.to_dict())
    else:
        append_jsonl(config.EQUITY_PATH, eq_row)

    # manage existing：短預算 book → 全倉 Book → ATR；force 以確保 scan 開頭必管一輪
    await manage_due_pass(gs, st, purpose="MANAGE", do_atr=True, force=True)

    # fetch & detect candidates（切片；到期插隊 manage；不背景續請求）
    candidates: list[dict[str, Any]] = []
    now_ms = int(time.time() * 1000)
    clock = get_due_clock()

    async def eval_symbol(sym: str) -> Optional[dict[str, Any]]:
        fetched_at = time.time()
        try:
            df, err = await await_scan_slice(fetch_klines_incremental(gs, sym))
        except asyncio.TimeoutError:
            return {"symbol": sym, "error": "SCAN_SLICE_TIMEOUT"}
        except asyncio.CancelledError:
            raise
        if err:
            return {"symbol": sym, "error": err}
        book = books.get(sym)
        probe = detect_breakout(df, now_ms=now_ms, book=book, data_fetched_at=fetched_at)
        return {"symbol": sym, "probe": probe, "book": book, "fetched_at": fetched_at}

    # 改為有界並發切片：每批發起前／後檢查 manage due，到期則插隊（不追趕積欠輪次）
    sem = asyncio.Semaphore(min(4, int(config.KLINES_API_CONCURRENCY)))
    results: list[Any] = []

    async def wrapped(sym: str):
        async with sem:
            if clock.is_due(time.monotonic()):
                await manage_due_pass(gs, st, purpose="MANAGE_DUE_PREEMPT", do_atr=False, force=False)
            return await eval_symbol(sym)

    # 分塊 gather，避免一次打滿且無法插隊
    chunk_sz = min(8, max(1, len(symbols)))
    for i in range(0, len(symbols), chunk_sz):
        if clock.is_due(time.monotonic()):
            await manage_due_pass(gs, st, purpose="MANAGE_DUE_PREEMPT", do_atr=False, force=False)
        chunk = symbols[i : i + chunk_sz]
        part = await asyncio.gather(*[wrapped(s) for s in chunk], return_exceptions=True)
        results.extend(part)

    for item in results:
        if isinstance(item, Exception):
            stats["failed"] += 1
            continue
        if not item:
            stats["failed"] += 1
            continue
        if item.get("error"):
            stats["failed"] += 1
            fail_row = {
                "event": "SCAN_FAIL",
                "symbol": item["symbol"],
                "error": item["error"],
                "ts": now_iso(),
                "run_id": st.run_id,
            }
            if store is not None:
                store.log_decision(fail_row)
            else:
                append_jsonl(config.DECISION_TRACE_PATH, fail_row)
            continue
        stats["success"] += 1
        probe = item["probe"]
        sym = item["symbol"]
        sig = probe.get("signal")
        tags = _source_tags(sym, gainers, losers, bool(sig) or (probe.get("vol_metric") or {}).get("vol_x", 0) >= config.VOL_X_MIN)
        # archive closed bars used for eval
        if store is not None and item.get("book"):
            store.log_quote_event(
                {
                    "purpose": "SIGNAL_EVAL",
                    "symbol": sym,
                    "bid": item["book"].get("bid"),
                    "ask": item["book"].get("ask"),
                    "mid": item["book"].get("mid"),
                    "spread": item["book"].get("spread"),
                    "fetched_at": item["book"].get("fetched_at"),
                    "signal_open_time": probe.get("signal_open_time"),
                }
            )

        # reverse：先平倉（不受同批同向上限影響）；新開併入候選排序，再受同批同向上限
        pos = get_position(st, sym)
        if pos and sig and sig["side"] != pos.side:
            book = item["book"]
            if book:
                rev = execute_reverse(
                    st,
                    symbol=sym,
                    sig=sig,
                    book=book,
                    source_tags=tags,
                    store=store,
                    allow_reopen=False,
                )
                pending = rev.get("pending_reopen")
                if pending and pending.get("sig_key") not in st.processed_signals:
                    candidates.append(
                        {
                            "symbol": pending["symbol"],
                            "side": pending["side"],
                            "signal": pending["signal"],
                            "probe": probe,
                            "tags": pending.get("source_tags") or tags,
                            "vol_x": float(pending["vol_x"]),
                            "spread": float(pending["spread"]),
                            "sig_key": pending["sig_key"],
                            "linked_prev": pending.get("linked_prev"),
                            "bypass_cooldown": bool(pending.get("bypass_cooldown")),
                            "is_reverse_reopen": True,
                        }
                    )
            continue

        if not sig:
            fr = probe.get("first_reject_code")
            if fr and fr not in ("NO_BREAKOUT", "INSUFFICIENT_BARS"):
                stats["rejected"][fr] = stats["rejected"].get(fr, 0) + 1
            decision_id = f"dec:{sym}:{probe.get('signal_open_time')}:{fr or 'NONE'}:{int(time.time()*1000)}"
            eval_row = {
                "event": "SIGNAL_EVAL",
                "decision_id": decision_id,
                "symbol": sym,
                "ts": now_iso(),
                "run_id": st.run_id,
                "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
                "strategy_version": config.STRATEGY_VERSION,
                "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
                "source_tags": tags,
                "first_reject_code": fr,
                "block_reasons": [fr] if fr else [],
                "reject_category": "SIGNAL_NOT_MET" if fr else None,
                "result": probe.get("result"),
                "vol_metric": probe.get("vol_metric"),
                "range": probe.get("range"),
                "atr": probe.get("atr"),
                "conditions": probe.get("conditions"),
                "bars_raw": probe.get("bars_raw"),
                "bars_closed": probe.get("bars_closed"),
                "excluded_unclosed": probe.get("excluded_unclosed"),
                "signal_open_time": probe.get("signal_open_time"),
                "signal_key": (
                    f"{sym}:{probe.get('signal_open_time')}" if probe.get("signal_open_time") is not None else None
                ),
                "quote_fetched_at": (item.get("book") or {}).get("fetched_at"),
                "data_fetched_at": probe.get("data_fetched_at"),
                "final_action": "NO_ENTRY",
            }
            if store is not None:
                store.log_decision(eval_row)
                store.save_decision_snapshot(decision_id, {"probe": probe, "tags": tags})
            else:
                append_jsonl(config.DECISION_TRACE_PATH, eval_row)
            continue

        sk = _sig_key(sym, int(sig["signal_open_time"]), sig["side"])
        if sk in st.processed_signals:
            continue
        candidates.append(
            {
                "symbol": sym,
                "side": sig["side"],
                "signal": sig,
                "probe": probe,
                "tags": tags,
                "vol_x": float(sig["vol_x"]),
                "spread": float(sig["spread"]),
                "sig_key": sk,
            }
        )

    # sort: vol_x desc, spread asc, symbol asc（既有評分；同分固定可重現）
    candidates.sort(key=_candidate_rank_key)
    stats["candidates"] = len(candidates)
    ranked_log = {
        "event": "SCAN_BATCH_RANK",
        "scan_batch_id": scan_batch_id,
        "ts": now_iso(),
        "run_id": st.run_id,
        "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
        "rank_rule": "vol_x_desc,spread_asc,symbol_asc",
        "side_open_limit": SCAN_BATCH_SIDE_OPEN_LIMIT,
        "candidates": [
            {
                "rank": i + 1,
                "symbol": c["symbol"],
                "side": c["side"],
                "vol_x": c["vol_x"],
                "spread": c["spread"],
                "is_reverse_reopen": bool(c.get("is_reverse_reopen")),
                "sig_key": c["sig_key"],
            }
            for i, c in enumerate(candidates)
        ],
    }
    if store is not None:
        store.log_decision(ranked_log)
    else:
        append_jsonl(config.DECISION_TRACE_PATH, ranked_log)

    # prioritize focus pool order within same sort already global; focus only affects scan priority conceptually —
    # we already scanned all; opening uses deterministic sort.
    for c in candidates:
        sym = c["symbol"]
        side = c["side"]
        if sym in st.positions:
            continue
        decision_id = f"dec:{sym}:{c['signal']['signal_open_time']}:{side}"
        side_slot = batch_opened_by_side.get(side)
        if side_slot is not None and len(side_slot.get("trade_ids") or []) >= SCAN_BATCH_SIDE_OPEN_LIMIT:
            opened = {
                "ok": False,
                "block_reason": "SAME_SCAN_BATCH_SIDE_LIMIT",
                "block_reasons": ["SAME_SCAN_BATCH_SIDE_LIMIT"],
            }
            brs = ["SAME_SCAN_BATCH_SIDE_LIMIT"]
            cat = "SAME_SCAN_BATCH_SIDE_LIMIT"
            decision = {
                "event": "SIGNAL_EVAL",
                "decision_id": decision_id,
                "symbol": sym,
                "ts": now_iso(),
                "run_id": st.run_id,
                "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
                "strategy_version": config.STRATEGY_VERSION,
                "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
                "scan_batch_id": scan_batch_id,
                "source_tags": c["tags"],
                "side": side,
                "signal_key": c["sig_key"],
                "signal_open_time": c["signal"]["signal_open_time"],
                "vol_x": c["vol_x"],
                "spread": c["spread"],
                "atr": c["signal"]["atr"],
                "range": {"high": c["signal"]["range_high"], "low": c["signal"]["range_low"]},
                "est_fill": c["signal"]["est_fill_price"],
                "risk_estimate_u": None,
                "conditions": (c.get("probe") or {}).get("conditions"),
                "vol_metric": (c.get("probe") or {}).get("vol_metric"),
                "first_reject_code": "SAME_SCAN_BATCH_SIDE_LIMIT",
                "block_reasons": brs,
                "reject_category": cat,
                "final_action": "BLOCKED",
                "trade_id": None,
                "is_reverse_reopen": bool(c.get("is_reverse_reopen")),
                "batch_side_winner": side_slot.get("symbol"),
                "batch_side_winner_trade_id": (side_slot.get("trade_ids") or [None])[0],
                "rank_rule": "vol_x_desc,spread_asc,symbol_asc",
                "simulation_assumption": config.SIMULATION_ASSUMPTIONS,
            }
            if store is not None:
                store.log_decision(decision)
                store.save_decision_snapshot(
                    decision_id,
                    {"probe": c.get("probe"), "open_result": opened, "tags": c["tags"], "scan_batch_id": scan_batch_id},
                )
            else:
                append_jsonl(config.DECISION_TRACE_PATH, decision)
            st.processed_signals.append(c["sig_key"])
            stats["rejected"]["SAME_SCAN_BATCH_SIDE_LIMIT"] = (
                stats["rejected"].get("SAME_SCAN_BATCH_SIDE_LIMIT", 0) + 1
            )
            save_account(st)
            continue

        filt = (st.filters or {}).get(sym, {})
        open_kwargs: dict[str, Any] = {
            "symbol": sym,
            "side": side,
            "signal": c["signal"],
            "source_tags": c["tags"],
            "filters": filt,
        }
        if c.get("is_reverse_reopen"):
            open_kwargs["linked_prev"] = c.get("linked_prev")
            open_kwargs["bypass_cooldown"] = bool(c.get("bypass_cooldown", True))
        opened = try_open(st, **open_kwargs)
        brs = list(opened.get("block_reasons") or ([] if opened.get("ok") else [opened.get("block_reason")]))
        brs = [b for b in brs if b]
        cat = None
        if not opened.get("ok"):
            br0 = opened.get("block_reason")
            if br0 in ("RISK_BUDGET_EXCEEDED",):
                cat = "RISK_EXCEEDED"
            elif br0 in ("INSUFFICIENT_MARGIN",):
                cat = "MARGIN"
            elif br0 in ("MAX_POSITIONS",):
                cat = "MAX_POSITIONS"
            elif br0 in ("COOLDOWN",):
                cat = "COOLDOWN"
            elif br0 in ("DUPLICATE_SIGNAL",):
                cat = "DEDUP"
            elif br0 in ("DAILY_LOSS_LOCK",):
                cat = "DAILY_LOSS_LOCK"
            elif br0 in ("SAME_SYMBOL_POSITION",):
                cat = "SAME_SYMBOL"
            elif br0 in ("SAME_SCAN_BATCH_SIDE_LIMIT",):
                cat = "SAME_SCAN_BATCH_SIDE_LIMIT"
            else:
                cat = "OTHER_BLOCK"
        decision = {
            "event": "SIGNAL_EVAL",
            "decision_id": decision_id,
            "symbol": sym,
            "ts": now_iso(),
            "run_id": st.run_id,
            "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
            "strategy_version": config.STRATEGY_VERSION,
            "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
            "scan_batch_id": scan_batch_id,
            "source_tags": c["tags"],
            "side": side,
            "signal_key": c["sig_key"],
            "signal_open_time": c["signal"]["signal_open_time"],
            "vol_x": c["vol_x"],
            "spread": c["spread"],
            "atr": c["signal"]["atr"],
            "range": {"high": c["signal"]["range_high"], "low": c["signal"]["range_low"]},
            "est_fill": c["signal"]["est_fill_price"],
            "risk_estimate_u": opened.get("risk_estimate_u"),
            "conditions": (c.get("probe") or {}).get("conditions"),
            "vol_metric": (c.get("probe") or {}).get("vol_metric"),
            "first_reject_code": None if opened.get("ok") else opened.get("block_reason"),
            "block_reasons": brs,
            "reject_category": cat,
            "final_action": "OPEN" if opened.get("ok") else "BLOCKED",
            "trade_id": opened.get("trade_id"),
            "is_reverse_reopen": bool(c.get("is_reverse_reopen")),
            "rank_rule": "vol_x_desc,spread_asc,symbol_asc",
            "simulation_assumption": config.SIMULATION_ASSUMPTIONS,
        }
        if store is not None:
            store.log_decision(decision)
            store.save_decision_snapshot(
                decision_id,
                {"probe": c.get("probe"), "open_result": opened, "tags": c["tags"], "scan_batch_id": scan_batch_id},
            )
            if opened.get("ok"):
                store.log_equity(
                    {
                        "ts": now_iso(),
                        "equity": st.equity_mark(0.0),
                        "cash": st.cash_u,
                        "realized_net_u": st.realized_net_u,
                        "realized_fees_u": st.realized_fees_u,
                        "floating_u": 0.0,
                        "positions": len(st.positions),
                        "trade_id": opened.get("trade_id"),
                    },
                    reason="FILL",
                )
        else:
            append_jsonl(config.DECISION_TRACE_PATH, decision)
        st.processed_signals.append(c["sig_key"])
        if opened.get("ok"):
            stats["opened"] += 1
            batch_opened_by_side[side] = {
                "symbol": sym,
                "trade_ids": [opened.get("trade_id")],
                "vol_x": c["vol_x"],
                "spread": c["spread"],
                "is_reverse_reopen": bool(c.get("is_reverse_reopen")),
            }
            if c.get("is_reverse_reopen"):
                note_reverse(st, sym)
                rev_ok = {
                    "event": "REVERSE_OPEN",
                    "symbol": sym,
                    "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
                    "reverse_policy": "approved_full_breakout_reverse_reopen_20261119",
                    "trade_id": opened.get("trade_id"),
                    "new_side": side,
                    "ts": now_iso(),
                    "run_id": st.run_id,
                    "close_ok": True,
                    "open_ok": True,
                    "linked_prev_trade_id": c.get("linked_prev"),
                    "scan_batch_id": scan_batch_id,
                }
                if store is not None:
                    store.log_decision(rev_ok)
                else:
                    append_jsonl(config.DECISION_TRACE_PATH, rev_ok)
            console_zh.print_open_event(sym, side, opened.get("trade_id"))
        else:
            br = opened.get("block_reason") or "UNKNOWN"
            stats["rejected"][br] = stats["rejected"].get(br, 0) + 1
            if c.get("is_reverse_reopen"):
                rev_rej = {
                    "event": "REVERSE_OPEN_REJECTED",
                    "symbol": sym,
                    "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
                    "reverse_policy": "approved_full_breakout_reverse_reopen_20261119",
                    "block_reason": opened.get("block_reason"),
                    "block_reasons": opened.get("block_reasons"),
                    "ts": now_iso(),
                    "run_id": st.run_id,
                    "close_ok": True,
                    "open_ok": False,
                    "scan_batch_id": scan_batch_id,
                }
                if store is not None:
                    store.log_decision(rev_rej)
                else:
                    append_jsonl(config.DECISION_TRACE_PATH, rev_rej)
        save_account(st)
        if store is not None:
            store.mirror_account(st.to_dict())
            store.write_data_quality_md()
        if len(st.positions) >= config.MAX_CONCURRENT_POSITIONS:
            break

    stats["duration_sec"] = round(time.time() - t0, 3)
    stats["coverage"] = {
        "expected": stats["expected"],
        "success": stats["success"],
        "failed": stats["failed"],
        "note": "failed symbols are NOT claimed scanned",
    }
    # 掃描結束中文現場（floating 用本輪 books）
    try:
        floating_end = 0.0
        for sym, pdct in list(st.positions.items()):
            b = books.get(sym)
            if not b:
                continue
            pos = get_position(st, sym)
            if pos is None:
                continue
            floating_end += estimate_net_unrealized(pos, mark_price_for_side(pos.side, b))
        console_zh.print_scan_summary(st, stats, floating_u=floating_end, title="掃描完成")
    except Exception:
        console_zh.print_scan_summary(st, stats, floating_u=0.0, title="掃描完成")
    return stats


async def maybe_refresh_universe(gs: GuardedSession, st, *, force: bool = False) -> None:
    if (
        not force
        and now_ts() - float(st.last_universe_refresh_ts or 0) < config.UNIVERSE_REFRESH_MINUTES * 60
    ):
        return
    snap = await refresh_universe(gs)
    st.universe = snap["eligible"]
    st.top_gainers = [x["symbol"] for x in snap["top_gainers"]]
    st.top_losers = [x["symbol"] for x in snap["top_losers"]]
    st.filters = snap["filters"]
    st.last_universe_refresh_ts = now_ts()
    save_account(st)
    store = get_store()
    if store is not None:
        store.log_universe(snap)
        store.mirror_account(st.to_dict())
        # seed any new symbols' warmup from vault
        store.seed_warmup_from_vault([r["symbol"] for r in st.universe])


async def run_paper_loop(
    *,
    hours: float = 6.0,
    once: bool = False,
    launch_command: Optional[list] = None,
) -> None:
    ensure_dirs()
    st = load_or_create_account()
    # New observability run dir (does not reset account capital)
    store = begin_run()
    st.run_id = store.run_id  # align account run_id with overnight archive
    save_account(st)
    store.snapshot_code_tree()
    store.write_code_hashes()
    store.write_launch_meta(
        command=launch_command or ["python3", "-m", "breakout_follow_v1.paper_main", "--paper"],
        hours=hours,
        once=once,
    )
    man = store.write_run_manifest(st.to_dict())
    write_run_manifest(st)  # keep root pointer too
    store.mirror_account(st.to_dict())  # live mirror only; account_at_start.json already frozen

    import os

    config.PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    store.path("paper.pid").write_text(str(os.getpid()), encoding="utf-8")

    timeout = aiohttp.ClientTimeout(total=float(config.HTTP_OP_BUDGET_SEC) + 5.0)
    connector = aiohttp.TCPConnector(limit=config.KLINES_API_CONCURRENCY + 4)
    start = time.time()
    last_review = 0.0

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        gs = GuardedSession(session)
        await maybe_refresh_universe(gs, st, force=True)
        # ensure warmup archive for current universe + backfill ≥100 bars (observability only)
        syms = [r["symbol"] for r in (st.universe or [])]
        store.seed_warmup_from_vault(syms)
        console_zh.print_boot_banner(st)
        print(f"[暖機] 正在補齊 {len(syms)} 個標的的 K 線（僅觀測，不改訊號門檻）…", flush=True)
        bf = await backfill_warmup_klines(gs, syms, min_bars=100)
        print(
            f"[暖機] 完成：成功 {bf.get('ok')}／失敗 {bf.get('fail')}／請求 {bf.get('requested')}",
            flush=True,
        )
        store.write_data_quality_md()
        print(
            f"[啟動] 合格標的 {len(st.universe)}｜本金基準 {st.initial_capital_u:.0f} U｜"
            f"run 目錄 {store.root}",
            flush=True,
        )
        first_stats = await scan_once(gs, st, {})
        write_review(st, first_stats, man, store=store)
        store.write_data_quality_md()
        if once:
            print("[結束] --once 單輪掃描完成。", flush=True)
            return

        while True:
            if hours > 0 and (time.time() - start) >= hours * 3600:
                print("[結束] 觀察時數已到；持倉維持不強制平倉。", flush=True)
                write_review(st, first_stats, man, store=store)
                store.write_data_quality_md()
                break
            # align near SCAN_SECOND
            now = time.time()
            sec = time.localtime(now).tm_sec
            wait = (60 - sec + config.SCAN_SECOND) % 60
            if wait == 0:
                wait = 60
            # mid-minute：依 MANAGE_DUE 插隊；attempt start 推進 due，不補跑積欠輪次
            slept = 0.0
            while slept < wait:
                clock = get_due_clock()
                now_m = time.monotonic()
                clock.ensure_init(now_m)
                until_due = clock.next_manage_due - now_m
                if until_due <= 0:
                    try:
                        await manage_due_pass(gs, st, purpose="MANAGE_POLL", do_atr=False, force=False)
                    except Exception as exc:
                        print(f"[警告] 持倉輪詢出錯：{exc}", flush=True)
                        store.note_quality("MANAGE_POLL_ERROR", detail=str(exc), replay_precise=False)
                    # 不追趕積欠：即使 manage 很久，下一輪仍等新的 next_manage_due
                    continue
                chunk = min(float(config.MANAGE_POLL_SEC), wait - slept, until_due)
                await asyncio.sleep(max(chunk, 0.01))
                slept += max(chunk, 0.01)

            try:
                await maybe_refresh_universe(gs, st)
                stats = await scan_once(gs, st, {})
                if time.time() - last_review >= config.UNIVERSE_REFRESH_MINUTES * 60:
                    write_review(st, stats, man, store=store)
                    store.write_data_quality_md()
                    last_review = time.time()
            except Exception as exc:
                print(f"[警告] 主迴圈出錯：{exc}", flush=True)
                store.note_quality("LOOP_ERROR", detail=str(exc), replay_precise=False)
                await asyncio.sleep(5)
