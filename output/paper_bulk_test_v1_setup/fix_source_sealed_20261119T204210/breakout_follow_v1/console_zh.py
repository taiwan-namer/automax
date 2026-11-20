"""終端機中文現場狀態（僅顯示，不改交易邏輯）。"""

from __future__ import annotations

import sys
from typing import Any, Optional

from . import config
from .account import get_position
from .portfolio import estimate_net_unrealized, mark_price_for_side
from .util import now_iso

# 常見拒絕碼 → 中文
REJECT_ZH = {
    "VOL_X": "量能不足",
    "NO_BREAKOUT": "無突破",
    "CHASE": "追價過遠",
    "FILL_NOT_BEYOND_BREAKOUT": "成交未越過突破",
    "STALE_KLINE": "K線過期",
    "SIGNAL_DELAY": "訊號延遲",
    "SPREAD": "點差過大",
    "RISK_BUDGET_EXCEEDED": "單筆風險超標",
    "INSUFFICIENT_MARGIN": "保證金不足",
    "MAX_POSITIONS": "已達持倉上限",
    "COOLDOWN": "冷卻中",
    "DUPLICATE_SIGNAL": "訊號重複",
    "DAILY_LOSS_LOCK": "日損鎖定禁新倉",
    "SAME_SYMBOL_POSITION": "同標的已有倉",
    "LOT_FILTER": "下單量過濾",
    "INSUFFICIENT_BARS": "K線不足",
}


def _flush(msg: str) -> None:
    print(msg, flush=True)


def reject_zh(code: Optional[str]) -> str:
    if not code:
        return "未知"
    return REJECT_ZH.get(str(code), str(code))


def format_rejects(rejected: dict[str, Any]) -> str:
    if not rejected:
        return "無"
    parts = []
    for k, v in sorted(rejected.items(), key=lambda x: (-int(x[1]), str(x[0]))):
        parts.append(f"{reject_zh(k)}×{v}")
    return "、".join(parts)


def floating_from_books(st: Any, books: Optional[dict] = None) -> float:
    if not books:
        return 0.0
    total = 0.0
    for sym, pdct in list(st.positions.items()):
        b = books.get(sym)
        if not b:
            continue
        pos = get_position(st, sym)
        if pos is None:
            continue
        mark = mark_price_for_side(pos.side, b)
        total += estimate_net_unrealized(pos, mark)
    return total


def print_wallet(st: Any, *, floating_u: float = 0.0, prefix: str = "[錢包]") -> None:
    equity = st.equity_mark(floating_u)
    n = len(st.positions)
    locked = "是" if st.day_new_entries_locked else "否"
    _flush(
        f"{prefix} {now_iso()}｜初始 {st.initial_capital_u:.2f} U｜"
        f"總權益 {equity:.2f} U（已實現 {st.realized_net_u:+.2f}／浮動 {floating_u:+.2f}）｜"
        f"現金 {st.cash_u:.2f}｜持倉 {n}/{config.MAX_CONCURRENT_POSITIONS}｜"
        f"日損鎖 {locked}（上限 {config.DAILY_LOSS_LOCK_USDT:.0f} U）"
    )


def print_positions(st: Any, books: Optional[dict] = None) -> None:
    if not st.positions:
        _flush("[持倉] 目前空手")
        return
    _flush("[持倉] 明細：")
    for sym in sorted(st.positions.keys()):
        pos = get_position(st, sym)
        if pos is None:
            continue
        side_zh = "多" if pos.side == "LONG" else "空"
        unrl = ""
        if books and books.get(sym):
            mark = mark_price_for_side(pos.side, books[sym])
            u = estimate_net_unrealized(pos, mark)
            unrl = f"｜標記約 {mark:.6g}｜浮動淨約 {u:+.2f} U"
        _flush(
            f"  · {sym} {side_zh}｜剩餘 {pos.qty_remaining}｜進場 {pos.entry_price:.6g}｜"
            f"有效停損 {pos.effective_stop:.6g}｜部分止盈={'已做' if pos.partial_tp_done else '尚未'}{unrl}"
        )


def print_scan_summary(st: Any, stats: dict[str, Any], *, floating_u: float = 0.0, title: str = "掃描") -> None:
    cov = stats.get("coverage") or {}
    rej = stats.get("rejected") or {}
    _flush("=" * 64)
    _flush(
        f"【{title}】{now_iso()}｜合格標的 {len(st.universe or [])}｜"
        f"掃描成功 {cov.get('success', stats.get('success'))}/"
        f"{cov.get('expected', stats.get('expected'))}｜"
        f"失敗 {cov.get('failed', stats.get('failed'))}｜"
        f"耗時 {stats.get('duration_sec')} 秒"
    )
    _flush(
        f"  候選 {stats.get('candidates', 0)}｜新開倉 {stats.get('opened', 0)}｜"
        f"拒絕：{format_rejects(rej)}"
    )
    print_wallet(st, floating_u=floating_u)
    print_positions(st)
    _flush("=" * 64)


def print_open_event(symbol: str, side: str, trade_id: Optional[str] = None) -> None:
    side_zh = "多" if side == "LONG" else "空"
    _flush(f"[開倉] {symbol} {side_zh}" + (f"｜單號 {trade_id}" if trade_id else ""))


def print_manage_events(symbol: str, events: list[dict[str, Any]]) -> None:
    for ev in events or []:
        action = ev.get("action") or ev.get("event") or "?"
        if action in ("PARTIAL_TP",) and ev.get("ok"):
            _flush(f"[部分止盈] {symbol} 成功｜數量 {ev.get('qty')}｜淨額約 {ev.get('net')}")
        elif action in ("TRAIL_STOP", "BREAKEVEN_STOP", "BREAKEVEN_STOP_IMMEDIATE", "INITIAL_STOP", "STOP"):
            _flush(f"[平倉] {symbol} 原因 {action}｜淨額約 {ev.get('net')}")
        elif action.endswith("_FAILED") or ev.get("ok") is False:
            _flush(f"[管理失敗] {symbol} {action}｜{ev.get('block_reason') or ev.get('error') or ''}")


def print_boot_banner(st: Any) -> None:
    _flush("=" * 64)
    _flush("啟動紙上 bulk 模擬（僅公開行情，禁止實盤下單）")
    _flush(f"設定檔：{getattr(config, 'PROFILE_ID', '')}｜版本標記：{getattr(config, 'CODE_FIX_TAG', '')}")
    _flush(f"本金基準 {st.initial_capital_u:.0f} U｜最多 {config.MAX_CONCURRENT_POSITIONS} 倉｜日損門檻 {config.DAILY_LOSS_LOCK_USDT:.0f} U")
    _flush(f"帳戶檔：{config.ACCOUNT_PATH}")
    _flush(f"本輪 run：{st.run_id}")
    _flush("提示：每分鐘會印中文【掃描】／【錢包】；Ctrl+C 優雅停止並保存帳戶（不平殺）。")
    _flush("=" * 64)
    print_wallet(st, floating_u=0.0)
    print_positions(st)
