"""管理到期批次：短預算 book、兩階段、完成事件、不補跑積欠輪次。

離線實作；不改止損／TP／反手／日損參數；不合入 N/T 禁止開倉。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import config, console_zh
from .market_data import fetch_book_tickers_manage, fetch_klines_incremental, wilder_atr, closed_klines
from .portfolio import manage_position
from .run_store import get_store
from .util import now_iso


MonoNow = Callable[[], float]


def _mono() -> float:
    return time.monotonic()


@dataclass
class ManageDueClock:
    """單調鐘 due：attempt start 時推進；禁止 while 追趕積欠輪次。"""

    next_manage_due: float = 0.0
    last_manage_attempt_started: float = 0.0
    initialized: bool = False

    def ensure_init(self, mono_now: float) -> None:
        if not self.initialized:
            self.last_manage_attempt_started = mono_now
            self.next_manage_due = mono_now  # 立刻允許第一次
            self.initialized = True

    def mark_attempt_start(self, mono_now: float) -> None:
        self.last_manage_attempt_started = mono_now
        self.next_manage_due = mono_now + float(config.MANAGE_DUE_SEC)

    def is_due(self, mono_now: float) -> bool:
        self.ensure_init(mono_now)
        return mono_now >= self.next_manage_due


# 模組級（單進程單迴圈）
_DUE = ManageDueClock()


def get_due_clock() -> ManageDueClock:
    return _DUE


def reset_due_clock_for_tests() -> None:
    global _DUE
    _DUE = ManageDueClock()


def _classify_symbol_outcome(events: List[dict[str, Any]], *, had_error: bool = False) -> str:
    if had_error:
        return "ERROR"
    if not events:
        return "NOOP"
    # 部分成功：PARTIAL ok 後續失敗 → 仍標 FILLED（並在 legs 保留失敗）
    filled = False
    op_fail = False
    for e in events:
        if e.get("ok") is True or e.get("action") in (
            "PARTIAL_TP",
            "INITIAL_STOP",
            "BREAKEVEN_STOP",
            "BREAKEVEN_STOP_IMMEDIATE",
            "TRAILING_STOP",
        ) and e.get("ok", True) is not False:
            if e.get("ok") is False or e.get("action", "").endswith("_FAILED"):
                op_fail = True
            else:
                # PARTIAL_TP without ok key treated via **r merge
                if e.get("action") == "PARTIAL_TP_FAILED":
                    op_fail = True
                elif e.get("ok") is True or (
                    e.get("action") in (
                        "PARTIAL_TP",
                        "INITIAL_STOP",
                        "BREAKEVEN_STOP",
                        "BREAKEVEN_STOP_IMMEDIATE",
                        "TRAILING_STOP",
                    )
                    and e.get("ok") is not False
                ):
                    filled = True
        if e.get("action") == "PARTIAL_TP_FAILED" or e.get("ok") is False:
            op_fail = True
    if filled and op_fail:
        return "FILLED"  # 部分操作成功；detail 含失敗腿
    if filled:
        return "FILLED"
    if op_fail:
        return "OP_FAIL"
    return "NOOP"


def _book_usable(book: Optional[dict[str, Any]]) -> bool:
    if not book:
        return False
    try:
        bid = float(book["bid"])
        ask = float(book["ask"])
    except Exception:
        return False
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    fetched_at = float(book.get("fetched_at") or 0)
    if fetched_at <= 0 or (time.time() - fetched_at) > config.QUOTE_MAX_AGE_SEC:
        return False
    return True


async def manage_due_pass(
    gs,
    st,
    *,
    purpose: str = "MANAGE_POLL",
    do_atr: bool = False,
    mono_now: Optional[MonoNow] = None,
    force: bool = False,
) -> dict[str, Any]:
    """一輪管理：短預算取 book → 全倉 Book 階段 →（可選）ATR 階段。

    - attempt start 即推進 next_manage_due（不在結束時補跑積欠）。
    - 整批 book 失敗仍寫 batch blocked／failed 事件。
    - 無持倉：expected_n=0，不冒充全倉成功。
    """
    mono = mono_now or _mono
    now = mono()
    clock = get_due_clock()
    clock.ensure_init(now)
    if not force and not clock.is_due(now):
        return {"skipped": True, "reason": "not_due", "next_manage_due": clock.next_manage_due}

    clock.mark_attempt_start(now)
    batch_id = uuid.uuid4().hex[:12]
    store = get_store()
    symbols = list(st.positions.keys())
    expected_n = len(symbols)
    batch: dict[str, Any] = {
        "event_type": "MANAGE_BATCH_COMPLETE",
        "batch_id": batch_id,
        "purpose": purpose,
        "ts": now_iso(),
        "expected_n": expected_n,
        "checked_n": 0,
        "success_n": 0,
        "blocked_n": 0,
        "error_n": 0,
        "filled_n": 0,
        "book_ok": False,
        "mono_attempt_start": now,
        "next_manage_due": clock.next_manage_due,
    }
    if expected_n == 0:
        batch["status"] = "NO_POSITIONS"
        batch["note"] = "expected_n=0; not claiming all-managed success"
        if store is not None:
            store.log_manage_event(batch)
        return batch

    books: dict[str, dict[str, Any]] = {}
    try:
        books = await fetch_book_tickers_manage(gs)
        batch["book_ok"] = True
    except Exception as exc:
        batch["status"] = "BATCH_BOOK_FAIL"
        batch["blocked_n"] = expected_n
        batch["detail"] = str(exc)[:400]
        if store is not None:
            store.note_quality("MANAGE_BOOK_GIVE_UP", detail=str(exc)[:300], replay_precise=False)
            store.log_manage_event(batch)
            for sym in symbols:
                store.log_manage_event(
                    {
                        "event_type": "MANAGE_SYMBOL_RESULT",
                        "batch_id": batch_id,
                        "attempt_id": f"{batch_id}:{sym}",
                        "symbol": sym,
                        "trade_id": (st.positions.get(sym) or {}).get("trade_id"),
                        "purpose": purpose,
                        "manage_outcome": "OP_FAIL",
                        "detail": "batch_book_fail",
                        "ts": now_iso(),
                    }
                )
        return batch

    # --- Book 階段（全倉，不 await ATR）---
    per: List[dict[str, Any]] = []
    for sym in list(symbols):
        attempt_id = f"{batch_id}:{sym}:book"
        row: dict[str, Any] = {
            "event_type": "MANAGE_SYMBOL_RESULT",
            "batch_id": batch_id,
            "attempt_id": attempt_id,
            "symbol": sym,
            "trade_id": (st.positions.get(sym) or {}).get("trade_id"),
            "purpose": purpose,
            "phase": "book",
            "ts": now_iso(),
            "legs": [],
        }
        batch["checked_n"] += 1
        try:
            b = books.get(sym)
            # 使用前再驗證
            if not _book_usable(b):
                row["manage_outcome"] = "OP_FAIL"
                row["detail"] = "QUOTE_STALE_OR_MISSING"
                batch["blocked_n"] += 1
                if store is not None:
                    store.note_quality(
                        "HIGH_PRIORITY_EXIT_MGMT_RISK",
                        symbol=sym,
                        detail="manage skipped: missing/stale/invalid quote; no fabricated exit",
                        replay_precise=False,
                    )
                    store.log_manage_event(row)
                per.append(row)
                continue
            if store is not None:
                store.log_quote_event(
                    {
                        "purpose": purpose,
                        "symbol": sym,
                        "bid": b.get("bid"),
                        "ask": b.get("ask"),
                        "mid": b.get("mid"),
                        "spread": b.get("spread"),
                        "fetched_at": b.get("fetched_at"),
                        "trade_id": row["trade_id"],
                        "batch_id": batch_id,
                    }
                )
            evs = manage_position(st, sym, book=b, atr_latest=None, phase="book")
            row["legs"] = evs
            outcome = _classify_symbol_outcome(evs)
            # 部分成功後失敗：legs 同時保留
            if any(e.get("action") == "PARTIAL_TP" and e.get("ok") is not False for e in evs) and any(
                e.get("ok") is False or e.get("action") == "PARTIAL_TP_FAILED" for e in evs
            ):
                row["partial_then_fail"] = True
            row["manage_outcome"] = outcome
            row["book_fetched_at"] = b.get("fetched_at")
            if outcome == "FILLED":
                batch["filled_n"] += 1
                batch["success_n"] += 1
            elif outcome == "NOOP":
                batch["success_n"] += 1
            elif outcome == "OP_FAIL":
                batch["blocked_n"] += 1
            elif outcome == "ERROR":
                batch["error_n"] += 1
            if evs:
                console_zh.print_manage_events(sym, evs)
        except Exception as mexc:
            row["manage_outcome"] = "ERROR"
            row["detail"] = str(mexc)[:300]
            batch["error_n"] += 1
            if store is not None:
                store.note_quality(
                    "MANAGE_SYMBOL_FAIL_BOUNDED",
                    symbol=sym,
                    detail=str(mexc)[:300],
                    replay_precise=False,
                )
        if store is not None:
            store.log_manage_event(row)
        per.append(row)

    # --- ATR 階段：僅仍持倉且 book 未過期；失敗不擋下一 due（due 已在 start 推進）---
    if do_atr:
        for sym in list(st.positions.keys()):
            attempt_id = f"{batch_id}:{sym}:atr"
            row = {
                "event_type": "MANAGE_SYMBOL_RESULT",
                "batch_id": batch_id,
                "attempt_id": attempt_id,
                "symbol": sym,
                "trade_id": (st.positions.get(sym) or {}).get("trade_id"),
                "purpose": purpose,
                "phase": "atr",
                "ts": now_iso(),
                "legs": [],
            }
            try:
                b = books.get(sym)
                if not _book_usable(b):
                    row["manage_outcome"] = "OP_FAIL"
                    row["detail"] = "QUOTE_STALE_AFTER_WAIT"
                    if store is not None:
                        store.log_manage_event(row)
                    continue
                atr = None
                try:
                    df, err = await fetch_klines_incremental(gs, sym)
                    if err is None and df is not None and not df.empty:
                        cl = closed_klines(df)
                        atr = wilder_atr(cl.tail(config.ATR_PERIOD + 5), config.ATR_PERIOD)
                        if store is not None and cl is not None and not cl.empty:
                            store.archive_klines(sym, cl, note="manage_closed")
                    elif err and store is not None:
                        store.note_quality(
                            "MANAGE_KLINE_FAIL_BOUNDED",
                            symbol=sym,
                            detail=str(err)[:300],
                            replay_precise=False,
                        )
                except Exception as kexc:
                    if store is not None:
                        store.note_quality(
                            "MANAGE_KLINE_FAIL_BOUNDED",
                            symbol=sym,
                            detail=str(kexc)[:300],
                            replay_precise=False,
                        )
                    # ATR 失敗：不清除既有 stop；跳過 trail 更新
                    row["manage_outcome"] = "NOOP"
                    row["detail"] = "ATR_FAIL_KEEP_STOPS"
                    if store is not None:
                        store.log_manage_event(row)
                    continue
                # 進 ATR 前再檢 book 年齡
                if not _book_usable(b):
                    row["manage_outcome"] = "OP_FAIL"
                    row["detail"] = "QUOTE_STALE_AFTER_WAIT"
                    if store is not None:
                        store.log_manage_event(row)
                    continue
                evs = manage_position(st, sym, book=b, atr_latest=atr, phase="atr")
                row["legs"] = evs
                row["manage_outcome"] = _classify_symbol_outcome(evs)
                row["book_fetched_at"] = b.get("fetched_at")
                if evs:
                    console_zh.print_manage_events(sym, evs)
            except Exception as mexc:
                row["manage_outcome"] = "ERROR"
                row["detail"] = str(mexc)[:300]
                if store is not None:
                    store.note_quality(
                        "MANAGE_SYMBOL_FAIL_BOUNDED",
                        symbol=sym,
                        detail=str(mexc)[:300],
                        replay_precise=False,
                    )
            if store is not None:
                store.log_manage_event(row)

    batch["status"] = "OK"
    batch["per_symbol_n"] = len(per)
    if store is not None:
        store.log_manage_event(batch)
        try:
            console_zh.print_wallet(
                st, floating_u=console_zh.floating_from_books(st, books), prefix="[盯倉]"
            )
        except Exception:
            pass
    return batch


async def await_scan_slice(coro, *, timeout: Optional[float] = None):
    """掃描切片：wait_for 逾時取消內層；不得背景續跑。

    注意：timeout 非硬牆鐘上界（取消收尾／同步 IO 另計）；fake clock ≠ 真實網路保證。
    """
    import asyncio

    t = float(config.SCAN_SLICE_MAX_WAIT_SEC if timeout is None else timeout)
    return await asyncio.wait_for(coro, timeout=t)
