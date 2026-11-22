"""D 終端機中文現場狀態（僅顯示，不改交易邏輯）。"""

from __future__ import annotations

from typing import Any, Optional

from . import config
from .portfolio import cover_exit_price, _pnl_for_mark
from .util import now_iso


def _flush(msg: str) -> None:
    print(msg, flush=True)


def floating_from_books(st: Any, books: Optional[dict] = None) -> float:
    """未實現淨（平空用 ask+滑價估算出場費後淨額）。無報價則該筆計 0。"""
    if not books:
        return 0.0
    total = 0.0
    for sym, pos in list((getattr(st, "positions", None) or {}).items()):
        b = books.get(sym)
        if not b:
            continue
        ask = float(b.get("ask") or 0)
        if ask <= 0:
            continue
        mark = cover_exit_price(ask)
        total += float(_pnl_for_mark(pos, mark)["net"])
    return total


def print_wallet(st: Any, *, books: Optional[dict] = None, prefix: str = "[錢包]") -> None:
    floating = floating_from_books(st, books)
    n_pos = len(getattr(st, "positions", None) or {})
    n_pend = len(getattr(st, "pending_limit_entries", None) or {})
    _flush(
        f"{prefix} {now_iso()}｜"
        f"已實現淨 {float(st.realized_net_u):+.2f}｜"
        f"未實現(浮動) {floating:+.2f}｜"
        f"現金 {float(st.cash_u):.2f}｜"
        f"持倉 {n_pos}｜掛單 {n_pend}｜"
        f"名額 {st.slot_count()}/{config.MAX_CONCURRENT_SLOTS}"
    )
    print_positions(st, books=books)
    print_pending_limits(st, books=books)


def print_positions(st: Any, books: Optional[dict] = None) -> None:
    positions = getattr(st, "positions", None) or {}
    if not positions:
        _flush("[持倉] 目前空手")
        return
    _flush("[持倉] 明細：")
    for sym in sorted(positions.keys()):
        pos = positions.get(sym) or {}
        entry = float(pos.get("entry_price") or 0)
        qty = float(pos.get("qty_remaining") or 0)
        tp = pos.get("tp_usdt")
        sl = pos.get("sl_usdt")
        unrl = ""
        if books and books.get(sym):
            ask = float(books[sym].get("ask") or 0)
            bid = float(books[sym].get("bid") or 0)
            mid = float(books[sym].get("mid") or 0) or ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0)
            if ask > 0:
                mark = cover_exit_price(ask)
                u = float(_pnl_for_mark(pos, mark)["net"])
                mid_s = f"{mid:.8g}" if mid > 0 else "?"
                unrl = f"｜現 mid {mid_s}｜估平倉 {mark:.8g}｜浮動淨 {u:+.2f} U"
        _flush(
            f"  · {sym} 空｜剩餘 {qty}｜進場 {entry:.8g}｜"
            f"毛TP {tp}／毛SL {sl}{unrl}"
        )


def print_pending_limits(st: Any, books: Optional[dict] = None) -> None:
    pending = getattr(st, "pending_limit_entries", None) or {}
    if not pending:
        _flush("[掛單] 目前無待成交限價")
        return
    thr = float(getattr(config, "CANCEL_DROP_FROM_P_PCT", 0.01) or 0.01)
    cancel_mult = 1.0 - thr
    _flush(
        f"[掛單] 待成交限價 {len(pending)} 張"
        f"（mid≤P×{cancel_mult:.2f} 撤單；無時間TTL）："
    )
    for sym in sorted(pending.keys()):
        pend = pending.get(sym) or {}
        P = pend.get("P")
        lim = pend.get("limit_px")
        qty = pend.get("qty_remaining", pend.get("qty_target"))
        P_s = f"{float(P):.8g}" if P is not None else "?"
        lim_s = f"{float(lim):.8g}" if lim is not None else "?"
        qty_s = f"{float(qty):.8g}" if qty is not None else "?"
        extra = ""
        if books and books.get(sym) and P is not None and float(P) > 0:
            try:
                bid = float(books[sym]["bid"])
                ask = float(books[sym]["ask"])
                mid = float(books[sym].get("mid") or 0) or ((bid + ask) / 2.0)
                # 相對固定 P：跌破門檻不利於掛空成交／觸發撤單
                drop = (float(P) - mid) / float(P)
                cancel_px = float(P) * cancel_mult
                extra = (
                    f"｜現 mid {mid:.8g}｜撤單線 {cancel_px:.8g}｜"
                    f"相對P跌幅 {drop*100:.2f}%"
                )
            except Exception:
                extra = ""
        _flush(f"  · {sym} 空｜參考P {P_s}｜限價 {lim_s}｜目標量 {qty_s}{extra}")


def print_limit_queued(pending: dict[str, Any]) -> None:
    sym = pending.get("symbol") or "?"
    P = pending.get("P")
    lim = pending.get("limit_px")
    qty = pending.get("qty_target")
    P_s = f"{float(P):.8g}" if P is not None else "?"
    lim_s = f"{float(lim):.8g}" if lim is not None else "?"
    qty_s = f"{float(qty):.8g}" if qty is not None else "?"
    thr = float(getattr(config, "CANCEL_DROP_FROM_P_PCT", 0.01) or 0.01)
    _flush(
        f"[掛限價] {sym} 空｜參考P {P_s}｜限價 {lim_s}｜量 {qty_s}｜"
        f"bid≥限價才進場；mid≤P×{1.0-thr:.2f} 撤單（無時間TTL）"
    )
