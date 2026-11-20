"""
即時模擬盯盤與複盤系統（Live Paper Trader）

僅使用幣安公開 REST API（無需 API Key）：
  - 監控已收盤 K 線訊號
  - 模擬開平倉、TP/SL/時間停損
  - 每 10 分鐘重刷榜單與安全換班（CLOSE_ONLY／持倉保護）

模式：
  --once  單次掃描驗證後結束（預設）
  --loop  24h 循環掛機
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import numpy as np
import pandas as pd

import config
from fetcher_and_analyzer import fetch_klines_df, run_analysis
from audit_observability import (
    append_decision_trace,
    append_trade_audit,
    archive_trade_klines,
    bootstrap_audit_outputs,
    classify_intrabar_exit,
    effective_sl_prices,
    ensure_run_manifest_for_current_strategy,
    get_run_id,
    get_strategy_version,
)
from signal_reject_diag import (
    FADE_COND_ORDER,
    MOMO_COND_ORDER,
    append_reject_diag_jsonl,
    begin_probe,
    build_vol_metric,
    current_probe,
    mark_not_evaluated,
    mark_signal_pass,
    pass_cond,
    reject,
    remaining_after,
    set_bar_meta,
    take_probe,
    warn_write_error,
)
from kline_slices_for_ai import (
    cache_entry_kline_slices_for_symbol,
    record_close_and_write,
    update_zone_b_and_write,
)
from report_generator import generate_report
from strategy_router import (
    SCHOOL_MEAN_REV,
    SCHOOL_SPIKE_FADE,
    SCHOOL_SPIKE_MOMO,
    SCHOOL_TREND,
    SCHOOL_SWING_REV,
    run_router,
)

# ANSI 終端色彩
C_RESET = "\033[0m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"

INTERVAL_MS = {"1m": 60_000, "15m": 900_000}

TRADE_LOG_COLUMNS = [
    "timestamp",
    "event",
    "symbol",
    "school",
    "side",
    "entry_price",
    "exit_price",
    "tp_pct",
    "sl_pct",
    "max_hold_mins",
    "hold_mins",
    "margin_u",
    "leverage",
    "notional_u",
    "gross_pnl_u",
    "fee_u",
    "net_pnl_u",
    "roi_pct",
    "balance_after_u",
    "price_change_pct",
    "max_favorable_pct",
    "max_adverse_pct",
    "entry_signal_detail",
    "exit_reason",
    "entry_time",
    "trade_no",
    "reason",  # 相容舊欄位：OPEN=訊號細節；CLOSE=出場原因別名
    "mode",
]


@dataclass
class Position:
    symbol: str
    school: str
    side: str  # LONG / SHORT
    entry_price: float
    entry_time: float  # unix sec
    tp_pct: float
    sl_pct: float
    max_hold_mins: int
    notional: float = config.NOTIONAL_USDT
    margin: float = config.MARGIN_PER_TRADE_USDT
    leverage: float = config.LEVERAGE
    qty: float = 0.0
    last_price: float = 0.0
    close_only: bool = False
    entry_signal_detail: str = ""
    entry_time_str: str = ""
    max_favorable_pct: float = 0.0  # MFE（% 數值，例如 0.70 = 0.70%）
    max_adverse_pct: float = 0.0  # MAE（% 數值，正數表示不利幅度）
    # 開倉當下快取的 K 線切片（供平倉時組裝案發現場）
    kline_cache: dict[str, Any] = field(default_factory=dict)
    # 兩段式保本／鎖利狀態
    breakeven_armed: bool = False
    trail_armed: bool = False
    lock_offset_pct: float = 0.0  # 已上移保本距離（有利方向，%）
    price_position_pct: float = 50.0  # 開倉時價格位置（MOMO 方向過濾用）
    change_24h_pct: float = 0.0
    # 稽核：穩定 trade_id（不影響決策）
    trade_id: str = ""
    entry_run_id: str = "LEGACY_UNKNOWN"
    entry_strategy_version: str = "LEGACY_UNKNOWN"
    original_sl_pct: float = 0.0
    signal_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaperState:
    """帳戶狀態：realized 以淨損益累積；總權益 = 初始 + 已實現 + 浮動。"""

    realized_net_pnl: float = 0.0
    realized_fees: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    watchlist: dict[str, dict[str, Any]] = field(default_factory=dict)
    close_only: set[str] = field(default_factory=set)
    last_signal_keys: set[str] = field(default_factory=set)
    last_refresh_ts: float = 0.0
    trade_count: int = 0  # 已平倉編號計數（從歷史恢復）
    refresh_count: int = 0  # 榜單／股性重刷次數
    last_tradeable_symbols: list[str] = field(default_factory=list)
    last_refresh_time_str: str = "-"
    # symbol -> unix ts：該幣上次 SL_HIT 時間（冷卻期內禁止再開倉）
    sl_cooldown_until: dict[str, float] = field(default_factory=dict)

    @property
    def balance(self) -> float:
        """已實現權益（不含浮動）= 初始本金 + 累積已實現淨損益。"""
        return config.INITIAL_CAPITAL_USDT + self.realized_net_pnl

    def is_in_sl_cooldown(self, symbol: str) -> bool:
        until = self.sl_cooldown_until.get(symbol, 0.0)
        return time.time() < until

    def mark_sl_cooldown(self, symbol: str) -> None:
        mins = float(getattr(config, "SL_COOLDOWN_MINUTES", 15))
        self.sl_cooldown_until[symbol] = time.time() + mins * 60.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _closed_klines(df: pd.DataFrame, interval: str, now_ms: Optional[int] = None) -> pd.DataFrame:
    """排除尚未收盤的最後一根 K 線。"""
    if df.empty:
        return df
    now_ms = now_ms or int(time.time() * 1000)
    interval_ms = INTERVAL_MS[interval]
    mask = df["open_time"].astype(np.int64) + interval_ms <= now_ms
    return df.loc[mask].reset_index(drop=True)


def _wick_ratios(o: float, h: float, l: float, c: float) -> tuple[float, float, float]:
    """回傳 (上影線比例, 下影線比例, 實體佔比)，相對 high-low。"""
    rng = h - l
    if rng <= 0:
        return 0.0, 0.0, 0.0
    upper = h - max(o, c)
    lower = min(o, c) - l
    body = abs(c - o)
    return upper / rng, lower / rng, body / rng


def _price_change_pct(pos: Position, mark_price: float) -> float:
    """價格變動百分比（小數，例如 +0.01 = +1%）；做空取反向。"""
    if pos.entry_price <= 0:
        return 0.0
    raw = (mark_price - pos.entry_price) / pos.entry_price
    return raw if pos.side == "LONG" else -raw


def _calc_pnl(pos: Position, mark_price: float) -> tuple[float, float, float, float]:
    """
    回傳 (gross_pnl_u, fee_u, net_pnl_u, roi_pct)。
    Gross = NOTIONAL × 價格變動%；Fee = NOTIONAL × 0.001；Net = Gross - Fee。
    """
    pct = _price_change_pct(pos, mark_price)
    gross = pos.notional * pct
    fee = pos.notional * config.ROUND_TRIP_FEE_RATE
    net = gross - fee
    roi = (net / pos.margin * 100.0) if pos.margin else 0.0
    return gross, fee, net, roi


def calc_floating_net(state: PaperState) -> float:
    """當前持倉浮動淨損益（以 last_price 估價，含預估雙邊手續費）。"""
    total = 0.0
    for pos in state.positions.values():
        mark = pos.last_price if pos.last_price > 0 else pos.entry_price
        _, _, net, _ = _calc_pnl(pos, mark)
        total += net
    return total


def calc_equity(state: PaperState) -> tuple[float, float, float]:
    """回傳 (總權益, 已實現淨損益, 浮動淨損益)。"""
    realized = state.realized_net_pnl
    floating = calc_floating_net(state)
    equity = config.INITIAL_CAPITAL_USDT + realized + floating
    return equity, realized, floating


def print_wallet_status(state: PaperState) -> None:
    """位置 A：終端機每分鐘狀態列。"""
    equity, realized, floating = calc_equity(state)
    n = len(state.positions)
    print(
        f"{C_BOLD}[錢包] 初始: {config.INITIAL_CAPITAL_USDT:.2f} U | "
        f"目前總權益: {equity:.2f} U "
        f"(已實現: {realized:+.2f} U / 浮動: {floating:+.2f} U) | "
        f"持倉: {n}/{config.MAX_CONCURRENT_POSITIONS} 筆 "
        f"(單筆 {config.MARGIN_PER_TRADE_USDT:.0f}U x {config.LEVERAGE:.0f}x){C_RESET}"
    )


def _normalize_exit_reason(raw: object) -> str:
    """統一出場原因代碼。"""
    s = str(raw or "").strip().upper()
    mapping = {
        "TAKE_PROFIT": "TP_HIT",
        "TP_HIT": "TP_HIT",
        "TP": "TP_HIT",
        "STOP_LOSS": "SL_HIT",
        "SL_HIT": "SL_HIT",
        "SL": "SL_HIT",
        "TIME_EXIT": "TIME_EXIT",
        "TIME": "TIME_EXIT",
        "TRAIL_PROFIT": "TRAIL_PROFIT",
        "TRAIL": "TRAIL_PROFIT",
        "MOMO_STALL_EXIT": "MOMO_STALL_EXIT",
        "MOMO_STALL": "MOMO_STALL_EXIT",
        "BREAKEVEN_WIN": "BREAKEVEN_WIN",
        "BREAKEVEN_SL": "BREAKEVEN_WIN",
        "BE_WIN": "BREAKEVEN_WIN",
    }
    return mapping.get(s, s if s else "-")


def _ensure_trades_dataframe() -> pd.DataFrame:
    """
    讀取既有 live_trades_log.csv，相容舊欄位並補齊新欄位預設值。
    不會刪除歷史列（保留已實現損益）。
    """
    path = Path(config.LIVE_TRADES_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=TRADE_LOG_COLUMNS)

    df = pd.read_csv(path)
    # 舊 reason → 新欄位推導
    if "entry_signal_detail" not in df.columns:
        df["entry_signal_detail"] = "-"
    if "exit_reason" not in df.columns:
        df["exit_reason"] = "-"
    if "reason" in df.columns:
        open_mask = df["event"].astype(str).str.upper() == "OPEN"
        close_mask = df["event"].astype(str).str.upper() == "CLOSE"
        # OPEN 列的 reason 通常是進場訊號
        need_entry = open_mask & (
            df["entry_signal_detail"].isna()
            | (df["entry_signal_detail"].astype(str).isin(["", "-", "nan", "None"]))
        )
        df.loc[need_entry, "entry_signal_detail"] = df.loc[need_entry, "reason"].fillna("-")
        # CLOSE 列的 reason 通常是出場原因
        need_exit = close_mask & (
            df["exit_reason"].isna()
            | (df["exit_reason"].astype(str).isin(["", "-", "nan", "None"]))
        )
        df.loc[need_exit, "exit_reason"] = df.loc[need_exit, "reason"].map(_normalize_exit_reason)

    for col in TRADE_LOG_COLUMNS:
        if col not in df.columns:
            df[col] = "-"

    # 為 CLOSE 補 trade_no（若缺則依時間序編號）
    close_idx = df.index[df["event"].astype(str).str.upper() == "CLOSE"].tolist()
    running = 0
    for i in close_idx:
        running += 1
        val = df.at[i, "trade_no"] if "trade_no" in df.columns else None
        if val is None or pd.isna(val) or str(val).strip() in ["", "-", "nan", "None"]:
            df.at[i, "trade_no"] = running

    # 把 CLOSE 的 entry_signal_detail 盡量從同幣種最近 OPEN 回填
    for i in close_idx:
        detail = str(df.at[i, "entry_signal_detail"])
        if detail in ["-", "", "nan", "None"]:
            sym = df.at[i, "symbol"]
            prior = df.loc[:i]
            prior_open = prior[
                (prior["event"].astype(str).str.upper() == "OPEN")
                & (prior["symbol"] == sym)
            ]
            if not prior_open.empty:
                src = prior_open.iloc[-1]
                cand = src.get("entry_signal_detail", "-")
                if str(cand) in ["-", "", "nan", "None"]:
                    cand = src.get("reason", "-")
                df.at[i, "entry_signal_detail"] = cand if pd.notna(cand) else "-"
        # entry_time 回填
        et = str(df.at[i, "entry_time"])
        if et in ["-", "", "nan", "None"]:
            sym = df.at[i, "symbol"]
            prior = df.loc[:i]
            prior_open = prior[
                (prior["event"].astype(str).str.upper() == "OPEN")
                & (prior["symbol"] == sym)
            ]
            if not prior_open.empty:
                df.at[i, "entry_time"] = prior_open.iloc[-1].get("timestamp", "-")

    # 正規化 exit_reason
    close_mask = df["event"].astype(str).str.upper() == "CLOSE"
    df.loc[close_mask, "exit_reason"] = df.loc[close_mask, "exit_reason"].map(_normalize_exit_reason)

    # 缺的 MFE/MAE/price_change 等保留為 "-"
    for col in ("max_favorable_pct", "max_adverse_pct", "price_change_pct"):
        df[col] = df[col].fillna("-")

    ordered = [c for c in TRADE_LOG_COLUMNS if c in df.columns]
    extras = [c for c in df.columns if c not in ordered]
    df = df[ordered + extras]
    df.to_csv(path, index=False)
    return df[TRADE_LOG_COLUMNS]


def _append_trade_log(row: dict[str, Any]) -> None:
    """追加一列交易紀錄；先遷移舊 CSV 欄位再寫入，絕不洗掉歷史。"""
    path = Path(config.LIVE_TRADES_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_trades_dataframe()
    write_header = not path.exists() or path.stat().st_size == 0
    # 再讀一次確認表頭
    if path.exists() and path.stat().st_size > 0:
        write_header = False
    payload = {k: row.get(k, "-") for k in TRADE_LOG_COLUMNS}
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(payload)


def restore_state_from_trades_log(state: PaperState) -> dict[str, Any]:
    """
    從既有 CSV 恢復已實現損益、手續費、平倉編號。
    回傳摘要 dict。
    """
    df = _ensure_trades_dataframe()
    if df.empty:
        return {"closed": 0, "realized_net": 0.0, "realized_fees": 0.0}

    closed = df[df["event"].astype(str).str.upper() == "CLOSE"].copy()
    if closed.empty:
        return {"closed": 0, "realized_net": 0.0, "realized_fees": 0.0}

    net = pd.to_numeric(closed["net_pnl_u"], errors="coerce").fillna(0.0)
    fee = pd.to_numeric(closed["fee_u"], errors="coerce").fillna(0.0)
    state.realized_net_pnl = float(net.sum())
    state.realized_fees = float(fee.sum())
    # trade_no 最大值
    nos = pd.to_numeric(closed["trade_no"], errors="coerce").dropna()
    state.trade_count = int(nos.max()) if len(nos) else len(closed)

    # 從近期 SL_HIT 還原冷卻：重啟後仍禁止同幣立即再開
    cooldown_mins = float(getattr(config, "SL_COOLDOWN_MINUTES", 15))
    cooldown_sec = cooldown_mins * 60.0
    now_ts = time.time()
    restored_cd = 0
    for _, r in closed.iterrows():
        reason = _normalize_exit_reason(str(r.get("exit_reason", "") or r.get("reason", "")))
        if reason != "SL_HIT":
            continue
        sym = str(r.get("symbol", "") or "").strip()
        if not sym:
            continue
        ts_raw = str(r.get("timestamp", "") or "").strip()
        try:
            # 日誌格式通常為 YYYY-MM-DD HH:MM:SS
            close_ts = time.mktime(time.strptime(ts_raw[:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        until = close_ts + cooldown_sec
        if until > now_ts:
            prev = state.sl_cooldown_until.get(sym, 0.0)
            if until > prev:
                state.sl_cooldown_until[sym] = until
                restored_cd += 1

    return {
        "closed": len(closed),
        "realized_net": state.realized_net_pnl,
        "realized_fees": state.realized_fees,
        "trade_count": state.trade_count,
        "sl_cooldowns": restored_cd,
    }


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        s = str(v).strip()
        if s in ["", "-", "nan", "None"]:
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def _load_closed_trades() -> pd.DataFrame:
    """回傳已平倉明細（正規化後）。"""
    df = _ensure_trades_dataframe()
    if df.empty:
        return df
    closed = df[df["event"].astype(str).str.upper() == "CLOSE"].copy()
    if closed.empty:
        return closed
    for col in (
        "net_pnl_u",
        "gross_pnl_u",
        "fee_u",
        "roi_pct",
        "hold_mins",
        "entry_price",
        "exit_price",
        "tp_pct",
        "sl_pct",
        "price_change_pct",
        "max_favorable_pct",
        "max_adverse_pct",
        "margin_u",
        "notional_u",
        "leverage",
    ):
        if col in closed.columns:
            closed[col] = pd.to_numeric(closed[col], errors="coerce")
    closed["exit_reason"] = closed["exit_reason"].map(_normalize_exit_reason)
    closed["trade_no"] = pd.to_numeric(closed["trade_no"], errors="coerce")
    # 依時間排序
    if "timestamp" in closed.columns:
        closed = closed.sort_values("timestamp").reset_index(drop=True)
    # 補 trade_no
    if closed["trade_no"].isna().any():
        closed["trade_no"] = range(1, len(closed) + 1)
    return closed


def _load_trade_stats() -> dict[str, Any]:
    closed = _load_closed_trades()
    empty = {
        "total_closed": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "total_fees": 0.0,
        "max_win": 0.0,
        "max_loss": 0.0,
        "max_loss_label": "0.0000 U (無虧損)",
        "by_school": {},
        "by_coin": {},
        "closed_df": closed,
    }
    if closed.empty:
        return empty

    wins_df = closed[closed["net_pnl_u"] > 0]
    losses_df = closed[closed["net_pnl_u"] < 0]
    flats = closed[closed["net_pnl_u"] == 0]

    max_win = float(wins_df["net_pnl_u"].max()) if len(wins_df) else 0.0
    if len(losses_df):
        max_loss = float(losses_df["net_pnl_u"].min())
        max_loss_label = f"{max_loss:+.4f} U"
    else:
        max_loss = 0.0
        max_loss_label = "0.0000 U"

    by_school: dict[str, dict[str, Any]] = {}
    for school, g in closed.groupby("school"):
        by_school[str(school)] = _school_breakdown(g)

    by_coin: dict[str, dict[str, Any]] = {}
    for (sym, school), g in closed.groupby(["symbol", "school"]):
        by_coin[f"{sym}|{school}"] = _coin_breakdown(sym, school, g)

    return {
        "total_closed": len(closed),
        "wins": len(wins_df),
        "losses": len(losses_df),
        "flats": len(flats),
        "total_pnl": float(closed["net_pnl_u"].fillna(0).sum()),
        "total_fees": float(closed["fee_u"].fillna(0).sum()),
        "max_win": max_win,
        "max_loss": max_loss,
        "max_loss_label": max_loss_label,
        "by_school": by_school,
        "by_coin": by_coin,
        "closed_df": closed,
    }


def _exit_counts(g: pd.DataFrame) -> tuple[int, int, int]:
    reasons = g["exit_reason"].map(_normalize_exit_reason)
    tp = int((reasons == "TP_HIT").sum())
    sl = int((reasons == "SL_HIT").sum())
    tm = int((reasons == "TIME_EXIT").sum())
    return tp, sl, tm


def _school_breakdown(g: pd.DataFrame) -> dict[str, Any]:
    wins = g[g["net_pnl_u"] > 0]
    losses = g[g["net_pnl_u"] < 0]
    tp, sl, tm = _exit_counts(g)
    avg_win = float(wins["net_pnl_u"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["net_pnl_u"].mean()) if len(losses) else 0.0  # 負數
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else (float("inf") if avg_win > 0 else 0.0)
    return {
        "trades": len(g),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(g) * 100.0) if len(g) else 0.0,
        "tp": tp,
        "sl": sl,
        "time": tm,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "fees": float(g["fee_u"].fillna(0).sum()),
        "pnl": float(g["net_pnl_u"].fillna(0).sum()),
        "avg_hold": float(g["hold_mins"].fillna(0).mean()) if "hold_mins" in g else 0.0,
    }


def _coin_breakdown(symbol: str, school: str, g: pd.DataFrame) -> dict[str, Any]:
    wins = g[g["net_pnl_u"] > 0]
    tp, sl, tm = _exit_counts(g)
    longs = int((g["side"].astype(str).str.upper() == "LONG").sum())
    shorts = int((g["side"].astype(str).str.upper() == "SHORT").sum())
    return {
        "symbol": symbol,
        "school": school,
        "trades": len(g),
        "longs": longs,
        "shorts": shorts,
        "wins": len(wins),
        "win_rate": (len(wins) / len(g) * 100.0) if len(g) else 0.0,
        "tp": tp,
        "sl": sl,
        "time": tm,
        "pnl": float(g["net_pnl_u"].fillna(0).sum()),
    }


def write_daily_review(state: PaperState) -> Path:
    path = Path(config.DAILY_REVIEW_MD)
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = _load_trade_stats()
    equity, realized, floating = calc_equity(state)
    total_fees = state.realized_fees if abs(state.realized_fees) > 1e-12 else stats["total_fees"]
    # 以 CSV 為準校正已實現（避免 state 未恢復時顯示 0）
    if abs(state.realized_net_pnl) < 1e-12 and abs(stats["total_pnl"]) > 1e-12:
        realized = stats["total_pnl"]
        equity = config.INITIAL_CAPITAL_USDT + realized + floating
        total_fees = stats["total_fees"]

    equity_roi = (
        (equity - config.INITIAL_CAPITAL_USDT) / config.INITIAL_CAPITAL_USDT * 100.0
        if config.INITIAL_CAPITAL_USDT
        else 0.0
    )
    wr = (stats["wins"] / stats["total_closed"] * 100.0) if stats["total_closed"] else 0.0

    pool = state.last_tradeable_symbols or sorted(state.watchlist.keys())
    pool_s = ", ".join(pool) if pool else "（空）"
    protected = sorted(set(state.positions.keys()) | set(state.close_only))

    lines: list[str] = [
        "# 模擬盯盤・深度量化復盤報告（Daily Review for AI）",
        "",
        f"> 更新時間：{_now_str()}",
        f"> 未平倉：{len(state.positions)} / {config.MAX_CONCURRENT_POSITIONS} 筆",
        f"> 榜單重刷間隔：每 {config.REFRESH_ROUTING_MINUTES} 分鐘",
        f"> 目前已執行榜單重刷次數：{state.refresh_count}",
        f"> 最新一輪可交易池：{pool_s}",
        f"> 持倉保護（CLOSE_ONLY／未平倉續盯）：{', '.join(protected) if protected else '無'}",
        "",
        "## 帳戶資金總覽",
        "",
        "| 項目 | 數值 |",
        "| --- | --- |",
        f"| 初始本金 | {config.INITIAL_CAPITAL_USDT:.2f} U |",
        f"| 槓桿 | {config.LEVERAGE:.0f}x |",
        f"| 單筆保證金 | {config.MARGIN_PER_TRADE_USDT:.2f} U |",
        f"| 單筆名目價值 | {config.NOTIONAL_USDT:.2f} U |",
        f"| 累積手續費 | {total_fees:.4f} U |",
        f"| 累積淨損益（已實現） | {realized:+.4f} U |",
        f"| 浮動淨損益 | {floating:+.4f} U |",
        f"| 目前帳戶總權益 | {equity:.4f} U |",
        f"| 總資金報酬率 | {equity_roi:+.2f}% |",
        f"| 榜單重刷次數 | {state.refresh_count} |",
        f"| 最近重刷時間 | {state.last_refresh_time_str} |",
        f"| 最新可交易池 | {pool_s} |",
        "",
        "## 交易績效總覽",
        "",
        "| 指標 | 數值 |",
        "| --- | --- |",
        f"| 已平倉筆數 | {stats['total_closed']} |",
        f"| 勝 / 負 | {stats['wins']} / {stats['losses']} |",
        f"| 勝率 | {wr:.1f}% |",
        f"| 累積淨損益 | {stats['total_pnl']:+.4f} U |",
        f"| 最大獲利單 | {stats['max_win']:+.4f} U |",
        f"| 最大虧損單 | {stats['max_loss_label']} |",
        "",
        "## 一、各門派深度拆解表（Strategy Breakdown）",
        "",
    ]

    if not stats["by_school"]:
        lines.append("_尚無已平倉交易_")
        lines.append("")
    else:
        lines.append(
            "| 門派 | 總筆數 | 勝/負 | 勝率 | 出場結構(TP/SL/TIME) | 平均獲利(U) | 平均虧損(U) | 賺賠比 | 總手續費(U) | 淨損益(U) | 平均持倉(分) |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for school, s in sorted(stats["by_school"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            payoff = s["payoff"]
            payoff_s = "∞" if payoff == float("inf") else f"{payoff:.2f}"
            lines.append(
                f"| {school} | {s['trades']} | {s['wins']}/{s['losses']} | {s['win_rate']:.1f}% | "
                f"{s['tp']}/{s['sl']}/{s['time']} | {s['avg_win']:+.4f} | {s['avg_loss']:+.4f} | "
                f"{payoff_s} | {s['fees']:.4f} | {s['pnl']:+.4f} | {s['avg_hold']:.1f} |"
            )
        lines.append("")

    lines.append("## 二、各幣種實戰表現表（Per-Coin Breakdown）")
    lines.append("")
    if not stats["by_coin"]:
        lines.append("_尚無已平倉交易_")
        lines.append("")
    else:
        lines.append(
            "| 幣種 | 所屬門派 | 交易筆數 | 做多/做空 | 勝率 | 出場結構(TP/SL/TIME) | 累積淨損益(U) |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        coins = sorted(stats["by_coin"].values(), key=lambda x: x["pnl"], reverse=True)
        for c in coins:
            lines.append(
                f"| {c['symbol']} | {c['school']} | {c['trades']} | {c['longs']}/{c['shorts']} | "
                f"{c['win_rate']:.1f}% | {c['tp']}/{c['sl']}/{c['time']} | {c['pnl']:+.4f} |"
            )
        lines.append("")

    lines.append("## 三、最近 30 筆已平倉交易全息明細表（Trade-by-Trade X-Ray）")
    lines.append("")
    closed = stats["closed_df"]
    if closed is None or closed.empty:
        lines.append("_尚無已平倉交易_")
        lines.append("")
    else:
        recent = closed.tail(30)
        lines.append(
            "| # | 開倉~平倉時間 | 幣種 | 門派 | 方向 | 進場價 -> 出場價 | 幣價漲跌% | "
            "預設TP%/SL% | MFE%/MAE% | 出場原因 | 持倉(分) | 毛利(U) | 手續費(U) | 淨損益(U) | 進場觸發細節 |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        for _, r in recent.iterrows():
            tno = int(_to_float(r.get("trade_no"), 0)) or "-"
            entry_t = str(r.get("entry_time") or "-")
            exit_t = str(r.get("timestamp") or "-")
            if entry_t in ["-", "nan", "None"]:
                time_span = exit_t
            else:
                time_span = f"{entry_t} ~ {exit_t}"
            ep = _to_float(r.get("entry_price"))
            xp = _to_float(r.get("exit_price"))
            # 幣價漲跌%：優先用欄位，否則推算（未考慮方向的原始價差）
            pchg = r.get("price_change_pct")
            if pchg is None or str(pchg) in ["-", "nan", "None"] or (isinstance(pchg, float) and np.isnan(pchg)):
                side = str(r.get("side", "")).upper()
                if ep > 0 and xp > 0:
                    raw = (xp - ep) / ep * 100.0
                    pchg_v = raw if side == "LONG" else -raw
                else:
                    pchg_v = 0.0
            else:
                pchg_v = float(pchg)
            mfe = r.get("max_favorable_pct")
            mae = r.get("max_adverse_pct")
            mfe_s = "-" if mfe is None or str(mfe) in ["-", "nan", "None"] or (isinstance(mfe, float) and np.isnan(mfe)) else f"{float(mfe):.3f}%"
            mae_s = "-" if mae is None or str(mae) in ["-", "nan", "None"] or (isinstance(mae, float) and np.isnan(mae)) else f"{float(mae):.3f}%"
            detail = str(r.get("entry_signal_detail") or r.get("reason") or "-")
            detail = detail.replace("|", "/")
            lines.append(
                f"| {tno} | {time_span} | {r.get('symbol')} | {r.get('school')} | {r.get('side')} | "
                f"{ep:.8g} -> {xp:.8g} | {pchg_v:+.3f}% | "
                f"{_to_float(r.get('tp_pct')):.2f}% / {_to_float(r.get('sl_pct')):.2f}% | "
                f"{mfe_s} / {mae_s} | {_normalize_exit_reason(r.get('exit_reason'))} | "
                f"{_to_float(r.get('hold_mins')):.1f} | {_to_float(r.get('gross_pnl_u')):+.4f} | "
                f"{_to_float(r.get('fee_u')):.4f} | {_to_float(r.get('net_pnl_u')):+.4f} | {detail} |"
            )
        lines.append("")

    lines.append("## 目前未平倉")
    lines.append("")
    if not state.positions:
        lines.append("_無_")
        lines.append("")
    else:
        lines.append(
            "| 幣種 | 門派 | 方向 | 進場價 | 標記價 | 浮動淨損益 | ROI% | MFE%/MAE% | TP%/SL% | 已持倉(分) | 進場細節 | CLOSE_ONLY |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        now = time.time()
        for p in state.positions.values():
            hold = (now - p.entry_time) / 60.0
            mark = p.last_price if p.last_price > 0 else p.entry_price
            _, _, net, roi = _calc_pnl(p, mark)
            lines.append(
                f"| {p.symbol} | {p.school} | {p.side} | {p.entry_price} | {mark} | "
                f"{net:+.4f} | {roi:+.2f}% | {p.max_favorable_pct:.3f}%/{p.max_adverse_pct:.3f}% | "
                f"{p.tp_pct:.2f}%/{p.sl_pct:.2f}% | {hold:.1f} | "
                f"{(p.entry_signal_detail or '-').replace('|', '/')} | {p.close_only} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 給 AI 的深度複盤 Prompt",
            "",
            "```",
            "請根據以上深度量化復盤數據（含門派拆解、幣種表現、最近 30 筆全息明細、MFE/MAE）：",
            "1. 指出哪個門派／哪支幣表現最好與最差，並用出場結構(TP/SL/TIME)與賺賠比解釋原因；",
            "2. 根據 MFE/MAE 與預設 TP/SL，判斷停利是否過早、停損是否過寬或過窄；",
            "3. 建議調整哪些門檻或 TP/SL / max_hold 參數（請給具體數值）；",
            "4. 是否應暫時停用某個門派或某支幣；",
            "5. 對仍持倉的幣種給出盯盤注意事項。",
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 訊號偵測
# ---------------------------------------------------------------------------


def detect_mean_rev_15m(df_15m: pd.DataFrame) -> Optional[dict[str, Any]]:
    """連四反轉：連 4 紅+上影≥20%→空；連 4 黑+下影≥20%→多。"""
    closed = _closed_klines(df_15m, "15m")
    if len(closed) < 4:
        return None
    last4 = closed.tail(4)
    o = last4["open"].to_numpy(dtype=float)
    h = last4["high"].to_numpy(dtype=float)
    l = last4["low"].to_numpy(dtype=float)
    c = last4["close"].to_numpy(dtype=float)
    dirs = np.sign(c - o)
    if np.any(dirs == 0):
        return None
    up4, lo4, _ = _wick_ratios(o[-1], h[-1], l[-1], c[-1])
    entry = float(c[-1])
    key = f"MEAN_REV:{int(last4['open_time'].iloc[-1])}"
    # 累積漲跌：第1根開盤 → 第4根收盤
    if o[0] > 0:
        cum_pct = (c[-1] - o[0]) / o[0] * 100.0
    else:
        cum_pct = 0.0
    if np.all(dirs > 0) and up4 >= 0.20:
        detail = f"15m連4紅(累積{cum_pct:+.2f}%)+上影線{up4*100:.0f}%"
        return {"side": "SHORT", "entry": entry, "signal_key": key, "detail": detail}
    if np.all(dirs < 0) and lo4 >= 0.20:
        detail = f"15m連4黑(累積{cum_pct:+.2f}%)+下影線{lo4*100:.0f}%"
        return {"side": "LONG", "entry": entry, "signal_key": key, "detail": detail}
    return None


def _is_zombie_liquidity_1m(closed: pd.DataFrame, spike_idx: int) -> bool:
    """
    殭屍盤／流動性枯竭過濾：
    檢查爆量根之前 15 根已收盤 1m（不含爆量當根）。
    若 >=4 根振幅 <=0.05%，或平均振幅 <0.16% → 判定假爆量。
    """
    try:
        lookback = int(getattr(config, "ZOMBIE_LOOKBACK_1M", 15))
        flat_amp = float(getattr(config, "ZOMBIE_FLAT_AMP_PCT", 0.05))
        flat_need = int(getattr(config, "ZOMBIE_FLAT_COUNT_GE", 4))
        avg_lt = float(getattr(config, "ZOMBIE_AVG_AMP_LT", 0.16))
        if spike_idx < lookback:
            return False
        window = closed.iloc[spike_idx - lookback : spike_idx]
        if len(window) < lookback:
            return False
        o = window["open"].to_numpy(dtype=float)
        h = window["high"].to_numpy(dtype=float)
        l = window["low"].to_numpy(dtype=float)
        amps = np.where(o > 0, (h - l) / o * 100.0, 0.0)
        flat_n = int(np.sum(amps <= flat_amp))
        avg_amp = float(np.mean(amps)) if len(amps) else 0.0
        return flat_n >= flat_need or avg_amp < avg_lt
    except Exception:
        return False


def detect_spike_fade_1m(
    df_1m: pd.DataFrame,
    ctx: Optional[dict[str, Any]] = None,
    *,
    now_ms: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    T-1 爆量 + 騙炮反殺（極短線）：
      - 爆量倍數 5x~18x、影線 >= 40%、T 收反轉色
      - 方向限制：只准 LONG；SHORT 型態成立時回傳 None（FADE_SHORT_DISABLED）
    旁路診斷：寫入 current_probe()；K 線綁定維持 T-1 量倍／影線、T 確認與 entry。
    now_ms：僅供離線重現固定時間；live 不傳＝與原本相同。
    """
    probe = current_probe()
    closed = _closed_klines(df_1m, "1m", now_ms=now_ms)
    set_bar_meta(probe, df_raw=df_1m, closed=closed, interval_ms=INTERVAL_MS["1m"])
    if probe is not None:
        pass_cond(
            probe,
            "CLOSED_FILTER",
            actual={
                "bars_in": len(df_1m),
                "bars_closed": len(closed),
                "excluded": probe.get("excluded_unclosed_open_times"),
            },
            op="open_time+interval_ms<=now_ms",
            threshold="closed_only",
            unit="bars",
        )
    need = config.VOLUME_MA_PERIOD + 3
    if len(closed) < need:
        reject(
            probe,
            "INSUFFICIENT_CLOSED_BARS",
            actual=len(closed),
            op="<",
            threshold=need,
            unit="bars",
            detail={"VOLUME_MA_PERIOD": config.VOLUME_MA_PERIOD, "need_formula": "VOLUME_MA_PERIOD+3"},
            remaining=remaining_after(FADE_COND_ORDER, "INSUFFICIENT_CLOSED_BARS"),
        )
        return None
    pass_cond(
        probe,
        "INSUFFICIENT_CLOSED_BARS",
        actual=len(closed),
        op=">=",
        threshold=need,
        unit="bars",
    )
    vol = closed["volume"].to_numpy(dtype=float)
    o = closed["open"].to_numpy(dtype=float)
    h = closed["high"].to_numpy(dtype=float)
    l = closed["low"].to_numpy(dtype=float)
    c = closed["close"].to_numpy(dtype=float)
    i = len(closed) - 2  # T-1
    t = len(closed) - 1  # T
    set_bar_meta(
        probe, df_raw=df_1m, closed=closed, interval_ms=INTERVAL_MS["1m"], signal_idx=i
    )
    vol_ma = float(np.mean(vol[i - config.VOLUME_MA_PERIOD : i]))
    if vol_ma <= 0:
        if probe is not None:
            probe["vol_metric"] = build_vol_metric(
                spike_idx=i,
                spike_vol=float(vol[i]),
                vol_ma=vol_ma,
                vol_x=0.0,
                ma_period=config.VOLUME_MA_PERIOD,
                closed=closed,
                exclude_spike=True,
            )
        reject(
            probe,
            "VOL_MA_ZERO",
            actual=vol_ma,
            op="<=",
            threshold=0,
            unit="volume",
            detail={"denominator": f"mean(vol[{i - config.VOLUME_MA_PERIOD}:{i}]) excludes T-1"},
            remaining=remaining_after(FADE_COND_ORDER, "VOL_MA_ZERO"),
        )
        return None
    pass_cond(probe, "VOL_MA_ZERO", actual=vol_ma, op=">", threshold=0, unit="volume")
    vol_x = vol[i] / vol_ma
    if probe is not None:
        probe["vol_metric"] = build_vol_metric(
            spike_idx=i,
            spike_vol=float(vol[i]),
            vol_ma=vol_ma,
            vol_x=float(vol_x),
            ma_period=config.VOLUME_MA_PERIOD,
            closed=closed,
            exclude_spike=True,
        )
    vmin = float(getattr(config, "SPIKE_FADE_VOL_X_MIN", 5.0))
    vmax = float(getattr(config, "SPIKE_FADE_VOL_X_MAX", 18.0))
    if vol_x < vmin or vol_x > vmax:
        reject(
            probe,
            "VOL_X_RANGE",
            actual=float(vol_x),
            op="not_in",
            threshold=[vmin, vmax],
            unit="x_vs_prior_MA",
            detail={
                "spike_bar": "T-1",
                "spike_idx": i,
                "spike_volume": float(vol[i]),
                "vol_ma": vol_ma,
                "exclude_spike_from_denom": True,
            },
            remaining=remaining_after(FADE_COND_ORDER, "VOL_X_RANGE"),
        )
        return None
    pass_cond(
        probe,
        "VOL_X_RANGE",
        actual=float(vol_x),
        op="in",
        threshold=[vmin, vmax],
        unit="x_vs_prior_MA",
    )
    if _is_zombie_liquidity_1m(closed, i):
        reject(
            probe,
            "ZOMBIE_LIQUIDITY",
            actual=True,
            op="==",
            threshold=False,
            unit="bool",
            detail={"spike_idx": i, "lookback_excludes_spike": True},
            remaining=remaining_after(FADE_COND_ORDER, "ZOMBIE_LIQUIDITY"),
        )
        return None
    pass_cond(probe, "ZOMBIE_LIQUIDITY", actual=False, op="==", threshold=False, unit="bool")
    up, lo, _ = _wick_ratios(o[i], h[i], l[i], c[i])
    wick_min = float(getattr(config, "SPIKE_FADE_WICK_MIN", 0.40))
    t1_bull = c[i] > o[i]
    t1_bear = c[i] < o[i]
    t_bull = c[t] > o[t]
    t_bear = c[t] < o[t]
    entry = float(c[t])
    key = f"SPIKE_FADE:{int(closed['open_time'].iloc[t])}"
    ctx = ctx or {}
    chg24 = float(ctx.get("price_change_pct_24h") or ctx.get("change_24h_pct") or 0.0)
    pos_pct = float(ctx.get("price_position_pct") or 50.0)
    # 既定方向限制：SPIKE_FADE_1M 全面禁空（非績效調參）
    # 做空型態若成立 → 明確拒絕 FADE_SHORT_DISABLED，不得回傳 SHORT
    if t1_bull and up >= wick_min and t_bear:
        pass_cond(
            probe,
            "FADE_SHORT_PATTERN",
            actual={"t1_bull": t1_bull, "upper_wick": float(up), "t_bear": t_bear},
            op="AND",
            threshold={"wick_min": wick_min},
            unit="ratio",
        )
        reject(
            probe,
            "FADE_SHORT_DISABLED",
            actual={"side": "SHORT", "chg24": chg24, "pos_pct": pos_pct},
            op="==",
            threshold="LONG_ONLY",
            unit="policy",
            detail={
                "policy": "SPIKE_FADE_1M long-only",
                "would_have_entry": entry,
                "would_have_signal_key": key,
                "legacy_ban_short_superseded": True,
            },
            remaining=remaining_after(FADE_COND_ORDER, "FADE_SHORT_DISABLED"),
        )
        return None
    reject(
        probe,
        "FADE_SHORT_PATTERN",
        actual={
            "t1_bull": bool(t1_bull),
            "upper_wick": float(up),
            "wick_min": wick_min,
            "t_bear": bool(t_bear),
        },
        op="AND",
        threshold={"t1_bull": True, "upper_wick>=": wick_min, "t_bear": True},
        unit="ratio",
        detail={"note": "compound short path failed; long path still evaluated next"},
    )
    # short 型態未成立 → 方向禁空閘門未進入（不得標 FAIL）
    mark_not_evaluated(probe, ["FADE_SHORT_DISABLED"])
    # 做多：T-1 黑K + 下影線 AND T 收紅
    if t1_bear and lo >= wick_min and t_bull:
        pass_cond(
            probe,
            "FADE_LONG_PATTERN",
            actual={"t1_bear": t1_bear, "lower_wick": float(lo), "t_bull": t_bull},
            op="AND",
            threshold={"wick_min": wick_min},
            unit="ratio",
        )
        # 若 short pattern 已記 FAIL 但 long 成功，清除 first_reject
        if probe is not None:
            probe["first_reject_code"] = None
            probe["result"] = "SIGNAL"
        detail = f"T-1爆量{vol_x:.1f}x黑K+下影線{lo*100:.0f}%+T收紅"
        mark_signal_pass(probe, "LONG", entry, key)
        return {"side": "LONG", "entry": entry, "signal_key": key, "detail": detail}
    reject(
        probe,
        "FADE_LONG_PATTERN",
        actual={
            "t1_bear": bool(t1_bear),
            "lower_wick": float(lo),
            "wick_min": wick_min,
            "t_bull": bool(t_bull),
        },
        op="AND",
        threshold={"t1_bear": True, "lower_wick>=": wick_min, "t_bull": True},
        unit="ratio",
    )
    if probe is not None and probe.get("first_reject_code") == "FADE_SHORT_PATTERN":
        # 兩者皆失敗：保留第一拒絕 SHORT_PATTERN（順序影響）
        pass
    elif probe is not None and probe.get("first_reject_code") is None:
        probe["first_reject_code"] = "FADE_LONG_PATTERN"
        probe["result"] = "NO_SIGNAL"
    return None


def detect_spike_momo_1m(
    df_1m: pd.DataFrame,
    ctx: Optional[dict[str, Any]] = None,
    *,
    now_ms: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """爆量動能直追：5x~18x、實體≥65%；LONG 需位置≥50%、SHORT 需≤50%。
    旁路診斷：僅寫入 current_probe()，不改回傳型態與短路順序。
    now_ms：僅供離線重現固定時間；live 不傳＝與原本相同。
    """
    probe = current_probe()
    closed = _closed_klines(df_1m, "1m", now_ms=now_ms)
    set_bar_meta(probe, df_raw=df_1m, closed=closed, interval_ms=INTERVAL_MS["1m"])
    if probe is not None:
        pass_cond(
            probe,
            "CLOSED_FILTER",
            actual={
                "bars_in": len(df_1m),
                "bars_closed": len(closed),
                "excluded": probe.get("excluded_unclosed_open_times"),
            },
            op="open_time+interval_ms<=now_ms",
            threshold="closed_only",
            unit="bars",
        )
    need = config.VOLUME_MA_PERIOD + 2
    if len(closed) < need:
        reject(
            probe,
            "INSUFFICIENT_CLOSED_BARS",
            actual=len(closed),
            op="<",
            threshold=need,
            unit="bars",
            detail={"VOLUME_MA_PERIOD": config.VOLUME_MA_PERIOD, "need_formula": "VOLUME_MA_PERIOD+2"},
            remaining=remaining_after(MOMO_COND_ORDER, "INSUFFICIENT_CLOSED_BARS"),
        )
        return None
    pass_cond(
        probe,
        "INSUFFICIENT_CLOSED_BARS",
        actual=len(closed),
        op=">=",
        threshold=need,
        unit="bars",
    )
    vol = closed["volume"].to_numpy(dtype=float)
    o = closed["open"].to_numpy(dtype=float)
    h = closed["high"].to_numpy(dtype=float)
    l = closed["low"].to_numpy(dtype=float)
    c = closed["close"].to_numpy(dtype=float)
    i = len(closed) - 1
    set_bar_meta(
        probe, df_raw=df_1m, closed=closed, interval_ms=INTERVAL_MS["1m"], signal_idx=i
    )
    vol_ma = float(np.mean(vol[i - config.VOLUME_MA_PERIOD : i]))
    if vol_ma <= 0:
        if probe is not None:
            probe["vol_metric"] = build_vol_metric(
                spike_idx=i,
                spike_vol=float(vol[i]),
                vol_ma=vol_ma,
                vol_x=0.0,
                ma_period=config.VOLUME_MA_PERIOD,
                closed=closed,
                exclude_spike=True,
            )
        reject(
            probe,
            "VOL_MA_ZERO",
            actual=vol_ma,
            op="<=",
            threshold=0,
            unit="volume",
            remaining=remaining_after(MOMO_COND_ORDER, "VOL_MA_ZERO"),
        )
        return None
    pass_cond(probe, "VOL_MA_ZERO", actual=vol_ma, op=">", threshold=0, unit="volume")
    vol_x = vol[i] / vol_ma
    if probe is not None:
        probe["vol_metric"] = build_vol_metric(
            spike_idx=i,
            spike_vol=float(vol[i]),
            vol_ma=vol_ma,
            vol_x=float(vol_x),
            ma_period=config.VOLUME_MA_PERIOD,
            closed=closed,
            exclude_spike=True,
        )
    vmin = float(getattr(config, "SPIKE_MOMO_VOL_X_MIN", 5.0))
    vmax = float(getattr(config, "SPIKE_MOMO_VOL_X_MAX", 18.0))
    if vol_x < vmin or vol_x > vmax:
        reject(
            probe,
            "VOL_X_RANGE",
            actual=float(vol_x),
            op="not_in",
            threshold=[vmin, vmax],
            unit="x_vs_prior_MA",
            detail={
                "spike_bar": "last_closed",
                "spike_idx": i,
                "spike_volume": float(vol[i]),
                "vol_ma": vol_ma,
                "exclude_spike_from_denom": True,
            },
            remaining=remaining_after(MOMO_COND_ORDER, "VOL_X_RANGE"),
        )
        return None
    pass_cond(
        probe,
        "VOL_X_RANGE",
        actual=float(vol_x),
        op="in",
        threshold=[vmin, vmax],
        unit="x_vs_prior_MA",
    )
    if _is_zombie_liquidity_1m(closed, i):
        reject(
            probe,
            "ZOMBIE_LIQUIDITY",
            actual=True,
            op="==",
            threshold=False,
            unit="bool",
            detail={"spike_idx": i, "lookback_excludes_spike": True},
            remaining=remaining_after(MOMO_COND_ORDER, "ZOMBIE_LIQUIDITY"),
        )
        return None
    pass_cond(probe, "ZOMBIE_LIQUIDITY", actual=False, op="==", threshold=False, unit="bool")
    _, _, body_ratio = _wick_ratios(o[i], h[i], l[i], c[i])
    body_min = float(getattr(config, "SPIKE_MOMO_BODY_MIN", 0.65))
    if body_ratio < body_min:
        reject(
            probe,
            "BODY_RATIO",
            actual=float(body_ratio),
            op="<",
            threshold=body_min,
            unit="body/range",
            remaining=remaining_after(MOMO_COND_ORDER, "BODY_RATIO"),
        )
        return None
    pass_cond(
        probe,
        "BODY_RATIO",
        actual=float(body_ratio),
        op=">=",
        threshold=body_min,
        unit="body/range",
    )
    entry = float(c[i])
    key = f"SPIKE_MOMO:{int(closed['open_time'].iloc[i])}"
    ctx = ctx or {}
    pos_pct = float(ctx.get("price_position_pct") or 50.0)
    if c[i] > o[i]:
        if pos_pct < 50.0:
            reject(
                probe,
                "DIRECTION_POS_GATE",
                actual={"candle": "bull", "pos_pct": pos_pct},
                op="<",
                threshold=50.0,
                unit="pct",
                detail={"required_for_LONG": "pos_pct>=50"},
            )
            return None
        pass_cond(
            probe,
            "DIRECTION_POS_GATE",
            actual={"candle": "bull", "pos_pct": pos_pct, "side": "LONG"},
            op=">=",
            threshold=50.0,
            unit="pct",
        )
        detail = f"1m爆量{vol_x:.1f}x紅K+實體{body_ratio*100:.0f}%|位{pos_pct:.0f}%"
        mark_signal_pass(probe, "LONG", entry, key)
        return {"side": "LONG", "entry": entry, "signal_key": key, "detail": detail}
    if c[i] < o[i]:
        if pos_pct > 50.0:
            reject(
                probe,
                "DIRECTION_POS_GATE",
                actual={"candle": "bear", "pos_pct": pos_pct},
                op=">",
                threshold=50.0,
                unit="pct",
                detail={"required_for_SHORT": "pos_pct<=50"},
            )
            return None
        pass_cond(
            probe,
            "DIRECTION_POS_GATE",
            actual={"candle": "bear", "pos_pct": pos_pct, "side": "SHORT"},
            op="<=",
            threshold=50.0,
            unit="pct",
        )
        detail = f"1m爆量{vol_x:.1f}x黑K+實體{body_ratio*100:.0f}%|位{pos_pct:.0f}%"
        mark_signal_pass(probe, "SHORT", entry, key)
        return {"side": "SHORT", "entry": entry, "signal_key": key, "detail": detail}
    reject(
        probe,
        "DIRECTION_POS_GATE",
        actual={"candle": "doji", "pos_pct": pos_pct, "open": float(o[i]), "close": float(c[i])},
        op="==",
        threshold="no_direction",
        unit="ohlc",
    )
    return None


def detect_trend_follow_15m(df_15m: pd.DataFrame) -> Optional[dict[str, Any]]:
    """連三 + 量 > 20 均量順勢。"""
    closed = _closed_klines(df_15m, "15m")
    if len(closed) < config.VOLUME_MA_PERIOD + 3:
        return None
    last3 = closed.tail(3)
    o = last3["open"].to_numpy(dtype=float)
    c = last3["close"].to_numpy(dtype=float)
    dirs = np.sign(c - o)
    if np.any(dirs == 0) or not np.all(dirs == dirs[0]):
        return None
    vol = closed["volume"].to_numpy(dtype=float)
    i = len(closed) - 1
    vol_ma = float(np.mean(vol[i - config.VOLUME_MA_PERIOD : i]))
    if vol_ma <= 0 or vol[i] <= vol_ma:
        return None
    vol_x = vol[i] / vol_ma
    entry = float(c[-1])
    key = f"TREND:{int(closed['open_time'].iloc[-1])}"
    o0 = float(last3["open"].iloc[0])
    cum_pct = (entry - o0) / o0 * 100.0 if o0 > 0 else 0.0
    if dirs[0] > 0:
        detail = f"15m連3紅(累積{cum_pct:+.2f}%)+量{vol_x:.1f}x均量"
        return {"side": "LONG", "entry": entry, "signal_key": key, "detail": detail}
    detail = f"15m連3黑(累積{cum_pct:+.2f}%)+量{vol_x:.1f}x均量"
    return {"side": "SHORT", "entry": entry, "signal_key": key, "detail": detail}


def _swing_15m_gate(df_15m: pd.DataFrame, side: str) -> tuple[bool, str]:
    """
    SWING 15m 級別過濾與減速確認。
    回傳 (allowed, reason_suffix)。
    """
    try:
        closed = _closed_klines(df_15m, "15m")
        if len(closed) < 8:
            return False, "15m不足8根"
        last8 = closed.tail(8)
        o = last8["open"].to_numpy(dtype=float)
        h = last8["high"].to_numpy(dtype=float)
        l = last8["low"].to_numpy(dtype=float)
        c = last8["close"].to_numpy(dtype=float)
        red_n = int(np.sum(c >= o))
        black_n = int(np.sum(c < o))
        # 前一根 = last8 最後一根（已收盤）
        prev_o, prev_h, prev_l, prev_c = float(o[-1]), float(h[-1]), float(l[-1]), float(c[-1])
        up, lo, _ = _wick_ratios(prev_o, prev_h, prev_l, prev_c)
        prev_bear = prev_c < prev_o
        prev_bull = prev_c > prev_o

        if side == "SHORT":
            # 禁：主升段（>=6紅）或前一根長下影>=50%
            if red_n >= 6:
                return False, f"禁空:8根中{red_n}紅(主升)"
            if lo >= 0.50:
                return False, f"禁空:前15m下影{lo*100:.0f}%"
            # 減速：前一根收黑 或 上影>=35%
            if not (prev_bear or up >= 0.35):
                return False, "空缺減速:前15m未收黑且上影<35%"
            return True, f"15m減速OK(紅{red_n}/上影{up*100:.0f}%)"

        if side == "LONG":
            if black_n >= 6:
                return False, f"禁多:8根中{black_n}黑(主跌)"
            if up >= 0.50:
                return False, f"禁多:前15m上影{up*100:.0f}%"
            if not (prev_bull or lo >= 0.35):
                return False, "多缺減速:前15m未收紅且下影<35%"
            return True, f"15m減速OK(黑{black_n}/下影{lo*100:.0f}%)"
        return False, "未知方向"
    except Exception:
        return False, "15m閘門異常"


def detect_swing_rev_1m(
    df_1m: pd.DataFrame,
    df_15m: Optional[pd.DataFrame] = None,
) -> Optional[dict[str, Any]]:
    """
    門派5 SWING_REV_15M：右側結構破壞（延遲轉折）。
    需 15m 減速確認，禁止在強單邊主升／主跌段逆勢摸頭摸底。
    """
    closed = _closed_klines(df_1m, "1m")
    # 需要：當根 + 前 6 根 + 再往前 20 根均量參考 ≈ 27+
    if len(closed) < 28:
        return None

    o = closed["open"].to_numpy(dtype=float)
    h = closed["high"].to_numpy(dtype=float)
    l = closed["low"].to_numpy(dtype=float)
    c = closed["close"].to_numpy(dtype=float)
    vol = closed["volume"].to_numpy(dtype=float)
    n = len(closed)
    i = n - 1  # 當根已收盤

    # 過去 5 根：iloc[-6:-1] → indices i-5 … i-1
    w5_start, w5_end = i - 5, i  # [i-5, i)
    # 過去 6 根（不含當根）：iloc[-7:-1] → i-6 … i-1
    w6_start = i - 6
    # 過去 20 根（含這 5 根視窗）：iloc[-21:-1] → i-20 … i-1
    look20_start = i - 20

    if w6_start < 0 or look20_start < 0:
        return None

    highs_5 = h[w5_start:w5_end]
    lows_5 = l[w5_start:w5_end]
    vols_5 = vol[w5_start:w5_end]
    highs_20 = h[look20_start:w5_end]  # 過去 20 根已收盤（不含當根）
    lows_20 = l[look20_start:w5_end]

    vol_ma20 = float(np.mean(vol[look20_start:w5_end]))
    if vol_ma20 <= 0:
        return None
    had_spike = bool(np.any(vols_5 > vol_ma20 * 1.8))

    o6 = float(o[w6_start])
    c6 = float(c[i - 1])
    if o6 <= 0:
        return None
    cum6_pct = (c6 - o6) / o6 * 100.0

    cur_o, cur_h, cur_l, cur_c = float(o[i]), float(h[i]), float(l[i]), float(c[i])
    if cur_o <= 0:
        return None
    body_pct = abs(cur_c - cur_o) / cur_o * 100.0
    prev2_low = float(np.min(l[i - 2 : i]))
    prev2_high = float(np.max(h[i - 2 : i]))
    entry = cur_c
    key = f"SWING_REV:{int(closed['open_time'].iloc[i])}"

    cum_need = float(getattr(config, "SWING_CUM_MOVE_PCT", 1.8))
    df15 = df_15m if df_15m is not None else pd.DataFrame()

    # —— SHORT：衝高誘多後結構轉折 ——
    made_20_high = float(np.max(highs_5)) >= float(np.max(highs_20)) - 1e-12
    if (
        made_20_high
        and had_spike
        and cum6_pct >= cum_need
        and cur_c < cur_o
        and cur_c < prev2_low
        and body_pct >= 0.20
    ):
        ok, gate = _swing_15m_gate(df15, "SHORT")
        if not ok:
            return None
        vol_x = float(np.max(vols_5) / vol_ma20)
        detail = (
            f"右側轉折做空:近5根創20根新高+爆量{vol_x:.1f}x+"
            f"6根累積{cum6_pct:+.2f}%|當根收黑跌破前2低(body{body_pct:.2f}%)|{gate}"
        )
        return {"side": "SHORT", "entry": entry, "signal_key": key, "detail": detail}

    # —— LONG：急殺洗盤後結構轉折 ——
    made_20_low = float(np.min(lows_5)) <= float(np.min(lows_20)) + 1e-12
    if (
        made_20_low
        and had_spike
        and cum6_pct <= -cum_need
        and cur_c > cur_o
        and cur_c > prev2_high
        and body_pct >= 0.20
    ):
        ok, gate = _swing_15m_gate(df15, "LONG")
        if not ok:
            return None
        vol_x = float(np.max(vols_5) / vol_ma20)
        detail = (
            f"右側轉折做多:近5根創20根新低+爆量{vol_x:.1f}x+"
            f"6根累積{cum6_pct:+.2f}%|當根收紅突破前2高(body{body_pct:.2f}%)|{gate}"
        )
        return {"side": "LONG", "entry": entry, "signal_key": key, "detail": detail}

    return None


def detect_signal_for_school(
    school: str,
    df_1m: pd.DataFrame,
    df_15m: pd.DataFrame,
    ctx: Optional[dict[str, Any]] = None,
    *,
    now_ms: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    if school == SCHOOL_MEAN_REV:
        return detect_mean_rev_15m(df_15m)
    if school == SCHOOL_SPIKE_FADE:
        return detect_spike_fade_1m(df_1m, ctx=ctx, now_ms=now_ms)
    if school == SCHOOL_SPIKE_MOMO:
        return detect_spike_momo_1m(df_1m, ctx=ctx, now_ms=now_ms)
    if school == SCHOOL_TREND:
        return detect_trend_follow_15m(df_15m)
    if school == SCHOOL_SWING_REV:
        # 暫時封印：不開新倉
        if not bool(getattr(config, "SWING_REV_ENABLED", False)):
            return None
        return detect_swing_rev_1m(df_1m, df_15m)
    return None


# ---------------------------------------------------------------------------
# 開平倉
# ---------------------------------------------------------------------------


def _fmt_cooldown_until(state: PaperState, symbol: str) -> str:
    until = state.sl_cooldown_until.get(symbol, 0.0)
    if until <= 0:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(until))


def open_position(
    state: PaperState,
    symbol: str,
    school: str,
    side: str,
    entry: float,
    params: dict[str, float],
    reason: str,
    mode: str,
    df_1m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
    plan: Optional[dict[str, Any]] = None,
) -> Optional[Position]:
    if symbol in state.positions:
        return None
    # 防禦閘門：FADE 全面禁空（須在扣資金／建倉／寫成功 OPEN 之前）
    if school == SCHOOL_SPIKE_FADE and str(side).upper() == "SHORT":
        print(
            f"{C_YELLOW}[跳過] {symbol} SPIKE_FADE_1M 已全面禁空"
            f"（FADE_SHORT_DISABLED），拒絕 SHORT 開倉{C_RESET}"
        )
        try:
            append_trade_audit(
                {
                    "event": "OPEN_REJECTED",
                    "symbol": symbol,
                    "school": school,
                    "side": side,
                    "entry": entry,
                    "block_reason": "FADE_SHORT_DISABLED",
                    "note": "defense gate before margin/position/OPEN log",
                }
            )
        except Exception as exc:
            warn_write_error("open_position.FADE_SHORT_DISABLED", exc)
        return None
    # 封印 SWING：拒絕新開倉
    if school == SCHOOL_SWING_REV and not bool(getattr(config, "SWING_REV_ENABLED", False)):
        print(f"{C_YELLOW}[跳過] {symbol} SWING_REV 已暫時封印，不開新倉{C_RESET}")
        return None
    if state.is_in_sl_cooldown(symbol):
        rem = max(0.0, state.sl_cooldown_until.get(symbol, 0.0) - time.time())
        print(
            f"{C_YELLOW}[跳過] {symbol} 停損冷卻中"
            f"（剩餘 {rem/60.0:.1f}m，至 {_fmt_cooldown_until(state, symbol)}）{C_RESET}"
        )
        return None
    if len(state.positions) >= config.MAX_CONCURRENT_POSITIONS:
        print(
            f"{C_YELLOW}[跳過] 已達最大持倉 "
            f"{config.MAX_CONCURRENT_POSITIONS} 筆，無法開 {symbol}{C_RESET}"
        )
        return None

    used_margin = len(state.positions) * config.MARGIN_PER_TRADE_USDT
    free_equity = state.balance - used_margin
    if free_equity < config.MARGIN_PER_TRADE_USDT:
        print(
            f"{C_YELLOW}[跳過] 可用保證金不足開倉 {symbol}"
            f"（已實現權益 {state.balance:.2f}U，已占用 {used_margin:.0f}U）{C_RESET}"
        )
        return None

    notional = config.NOTIONAL_USDT
    qty = notional / entry if entry > 0 else 0.0
    entry_time_str = _now_str()
    entry_ts = time.time()
    plan = plan or {}

    # 開倉快取：進場前 8×15m + 10×1m（失敗不影響開倉）
    kline_cache: dict[str, Any] = {}
    try:
        kline_cache = cache_entry_kline_slices_for_symbol(
            symbol, df_1m=df_1m, df_15m=df_15m, entry_time_sec=entry_ts
        )
    except Exception:
        kline_cache = {}

    trade_id = f"{get_run_id()}:{symbol}:{int(entry_ts*1000)}"
    entry_ot = None
    try:
        entry_ot = (kline_cache or {}).get("entry_1m_open_time")
    except Exception:
        entry_ot = None
    signal_meta = {
        "signal_detail": reason,
        "entry_1m_open_time": entry_ot,
        "entry_obs_time": entry_time_str,
        "simulated_entry_price": entry,
        "price_position_pct": float(plan.get("price_position_pct") or 50.0),
        "price_change_pct_24h": float(plan.get("price_change_pct_24h") or 0.0),
        "primary_school": plan.get("primary_school") or school,
        "routing_status": plan.get("status") or "UNKNOWN",
        "routing_reason": None,
        "volume_metrics": "SEE_SIGNAL_DETAIL_OR_UNKNOWN",
        "unused_indicators": "NOT_USED",
    }
    # 從 plan.schools 找分流理由（若有）
    try:
        for m in plan.get("schools") or []:
            if m.get("school") == school:
                signal_meta["routing_reason"] = m.get("reason")
                break
    except Exception:
        pass

    pos = Position(
        symbol=symbol,
        school=school,
        side=side,
        entry_price=entry,
        entry_time=entry_ts,
        tp_pct=float(params["tp_pct"]),
        sl_pct=float(params["sl_pct"]),
        max_hold_mins=int(params["max_hold_mins"]),
        notional=notional,
        margin=config.MARGIN_PER_TRADE_USDT,
        leverage=config.LEVERAGE,
        qty=qty,
        last_price=entry,
        close_only=False,
        entry_signal_detail=reason,
        entry_time_str=entry_time_str,
        max_favorable_pct=0.0,
        max_adverse_pct=0.0,
        kline_cache=kline_cache,
        price_position_pct=float(plan.get("price_position_pct") or 50.0),
        change_24h_pct=float(plan.get("price_change_pct_24h") or 0.0),
        trade_id=trade_id,
        entry_run_id=get_run_id(),
        entry_strategy_version=get_strategy_version(),
        original_sl_pct=float(params["sl_pct"]),
        signal_meta=signal_meta,
    )
    state.positions[symbol] = pos

    try:
        append_trade_audit(
            {
                "event": "OPEN_SNAPSHOT",
                "trade_id": trade_id,
                "symbol": symbol,
                "school": school,
                "side": side,
                "entry_price_sim": entry,
                "tp_pct": pos.tp_pct,
                "sl_pct_original": pos.original_sl_pct,
                "max_hold_mins": pos.max_hold_mins,
                "notional_u": pos.notional,
                "signal_meta": signal_meta,
                "entry_run_id": pos.entry_run_id,
                "entry_strategy_version": pos.entry_strategy_version,
                "sl_levels": effective_sl_prices(
                    side=side,
                    entry=entry,
                    original_sl_pct=pos.original_sl_pct,
                    lock_offset_pct=0.0,
                    breakeven_armed=False,
                ),
            }
        )
        # 完整 K 線歸檔（進場前）
        archive_trade_klines(
            trade_id,
            symbol=symbol,
            pre_1m=(kline_cache or {}).get("pre_1m"),
            pre_15m=(kline_cache or {}).get("pre_15m"),
            hold_1m=None,
            post_1m=None,
            meta={"phase": "OPEN", "entry_1m_open_time": entry_ot},
        )
    except Exception:
        pass

    color = C_GREEN if side == "LONG" else C_RED
    print(
        f"{C_BOLD}{color}▶ OPEN {side} {symbol}{C_RESET} "
        f"| 門派 {school} | 進場 {entry:.8g} | 保證金 {pos.margin:.0f}U × {pos.leverage:.0f}x "
        f"| 名目 {pos.notional:.0f}U | TP {pos.tp_pct:.2f}% / SL {pos.sl_pct:.2f}% "
        f"| 最長 {pos.max_hold_mins}m | {reason}"
    )
    equity, _, _ = calc_equity(state)
    _append_trade_log(
        {
            "timestamp": entry_time_str,
            "event": "OPEN",
            "symbol": symbol,
            "school": school,
            "side": side,
            "entry_price": entry,
            "exit_price": "",
            "tp_pct": pos.tp_pct,
            "sl_pct": pos.sl_pct,
            "max_hold_mins": pos.max_hold_mins,
            "hold_mins": 0,
            "margin_u": pos.margin,
            "leverage": pos.leverage,
            "notional_u": pos.notional,
            "gross_pnl_u": "",
            "fee_u": "",
            "net_pnl_u": "",
            "roi_pct": "",
            "balance_after_u": round(equity, 6),
            "price_change_pct": "",
            "max_favorable_pct": 0.0,
            "max_adverse_pct": 0.0,
            "entry_signal_detail": reason,
            "exit_reason": "",
            "entry_time": entry_time_str,
            "trade_no": "",
            "reason": reason,
            "mode": mode,
        }
    )
    write_daily_review(state)
    print_wallet_status(state)
    return pos


def close_position(
    state: PaperState,
    symbol: str,
    exit_price: float,
    reason: str,
    mode: str,
) -> Optional[float]:
    pos = state.positions.get(symbol)
    if pos is None:
        return None

    exit_code = _normalize_exit_reason(reason)
    # 出場當下也更新一次 MFE/MAE
    _update_mfe_mae(pos, high=exit_price, low=exit_price)

    gross, fee, net, roi = _calc_pnl(pos, exit_price)
    state.realized_net_pnl += net
    state.realized_fees += fee
    hold_mins = (time.time() - pos.entry_time) / 60.0
    pct = _price_change_pct(pos, exit_price) * 100.0
    state.trade_count += 1
    trade_no = state.trade_count
    exit_time_str = _now_str()

    color = C_GREEN if net >= 0 else C_RED
    print(
        f"{C_BOLD}{color}■ CLOSE {pos.side} {symbol}{C_RESET} "
        f"| {exit_code} | 進場 {pos.entry_price:.8g} → 出場 {exit_price:.8g} ({pct:+.3f}%) "
        f"| MFE {pos.max_favorable_pct:.3f}% / MAE {pos.max_adverse_pct:.3f}% "
        f"| Gross {gross:+.4f}U - Fee {fee:.2f}U = Net {net:+.4f}U "
        f"| ROI {roi:+.2f}% | 持倉 {hold_mins:.1f}m"
    )
    entry_time_str = pos.entry_time_str or "-"
    entry_detail = pos.entry_signal_detail or "-"
    del state.positions[symbol]
    state.close_only.discard(symbol)

    # SL_HIT 後強制冷卻，同幣種一段時間內禁止再開倉
    if exit_code == "SL_HIT":
        state.mark_sl_cooldown(symbol)
        mins = float(getattr(config, "SL_COOLDOWN_MINUTES", 15))
        print(
            f"{C_YELLOW}  ⏳ {symbol} SL_HIT 冷卻 {mins:.0f} 分鐘"
            f"（至 {_fmt_cooldown_until(state, symbol)}）{C_RESET}"
        )

    equity, _, _ = calc_equity(state)
    _append_trade_log(
        {
            "timestamp": exit_time_str,
            "event": "CLOSE",
            "symbol": symbol,
            "school": pos.school,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "tp_pct": pos.tp_pct,
            "sl_pct": pos.sl_pct,
            "max_hold_mins": pos.max_hold_mins,
            "hold_mins": round(hold_mins, 2),
            "margin_u": pos.margin,
            "leverage": pos.leverage,
            "notional_u": pos.notional,
            "gross_pnl_u": round(gross, 6),
            "fee_u": round(fee, 6),
            "net_pnl_u": round(net, 6),
            "roi_pct": round(roi, 4),
            "balance_after_u": round(equity, 6),
            "price_change_pct": round(pct, 6),
            "max_favorable_pct": round(pos.max_favorable_pct, 6),
            "max_adverse_pct": round(pos.max_adverse_pct, 6),
            "entry_signal_detail": entry_detail,
            "exit_reason": exit_code,
            "entry_time": entry_time_str,
            "trade_no": trade_no,
            "reason": exit_code,
            "mode": mode,
        }
    )
    write_daily_review(state)
    # A 區：平倉案發現場 K 線切片（失敗不影響主迴圈）
    try:
        record_close_and_write(
            trade_no=trade_no,
            symbol=symbol,
            school=pos.school,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            exit_reason=exit_code,
            net_pnl=net,
            hold_mins=hold_mins,
            entry_time_str=entry_time_str,
            exit_time_str=exit_time_str,
            entry_detail=entry_detail,
            kline_cache=getattr(pos, "kline_cache", {}) or {},
            entry_time_sec=float(pos.entry_time),
            exit_time_sec=time.time(),
        )
    except Exception as exc:
        print(f"{C_DIM}  （K線切片寫入略過：{exc}）{C_RESET}")
    # 稽核 CLOSE（不改歷史成交）
    try:
        append_trade_audit(
            {
                "event": "CLOSE_SNAPSHOT",
                "trade_id": getattr(pos, "trade_id", "") or "UNKNOWN",
                "trade_no": trade_no,
                "symbol": symbol,
                "school": pos.school,
                "side": pos.side,
                "exit_reason_label": exit_code,
                "entry_run_id": getattr(pos, "entry_run_id", "LEGACY_UNKNOWN"),
                "entry_strategy_version": getattr(pos, "entry_strategy_version", "LEGACY_UNKNOWN"),
                "exit_run_id": get_run_id(),
                "exit_strategy_version": get_strategy_version(),
                "entry_price_sim": pos.entry_price,
                "exit_price_sim": exit_price,
                "gross_pnl_u": gross,
                "fee_u_round_trip": fee,
                "net_pnl_u": net,
                "mfe": pos.max_favorable_pct,
                "mae": pos.max_adverse_pct,
                "hold_mins": hold_mins,
                "balance_after_u_equity": equity,
                "balance_semantics": "INITIAL + realized_net + floating_at_log_time",
                "note": "prices are simulated fills, not exchange fills",
            }
        )
        archive_trade_klines(
            getattr(pos, "trade_id", None) or f"trade_{trade_no}",
            symbol=symbol,
            pre_1m=(getattr(pos, "kline_cache", {}) or {}).get("pre_1m"),
            pre_15m=(getattr(pos, "kline_cache", {}) or {}).get("pre_15m"),
            hold_1m=None,
            post_1m=None,
            meta={
                "phase": "CLOSE",
                "exit_reason": exit_code,
                "post_1m": "PENDING",
            },
        )
    except Exception:
        pass
    print_wallet_status(state)
    return net


def _update_mfe_mae(pos: Position, high: float, low: float) -> None:
    """用當根高低價更新持倉期間 MFE / MAE（百分比數值）。"""
    if pos.entry_price <= 0:
        return
    if pos.side == "LONG":
        fav = (high - pos.entry_price) / pos.entry_price * 100.0
        adv = (pos.entry_price - low) / pos.entry_price * 100.0
    else:
        fav = (pos.entry_price - low) / pos.entry_price * 100.0
        adv = (high - pos.entry_price) / pos.entry_price * 100.0
    pos.max_favorable_pct = max(pos.max_favorable_pct, max(fav, 0.0))
    pos.max_adverse_pct = max(pos.max_adverse_pct, max(adv, 0.0))


def _school_lock_params(school: str) -> dict[str, float]:
    """依門派回傳保本／鎖利門檻（支援多段 lock）。"""
    if school == SCHOOL_SPIKE_FADE:
        return {
            "be1_mfe": float(config.SPIKE_FADE_BE1_MFE),
            "be1_lock": float(config.SPIKE_FADE_BE1_LOCK),
            "be2_mfe": float(config.SPIKE_FADE_BE2_MFE),
            "be2_lock": float(config.SPIKE_FADE_BE2_LOCK),
            "trail_mfe": 99.0,  # FADE 極短線不靠回撤鎖利
            "trail_give": 99.0,
        }
    if school == SCHOOL_SPIKE_MOMO:
        return {
            "be1_mfe": float(config.SPIKE_MOMO_BE_MFE),
            "be1_lock": float(config.SPIKE_MOMO_BE_LOCK),
            "be2_mfe": 99.0,
            "be2_lock": float(config.SPIKE_MOMO_BE_LOCK),
            "trail_mfe": float(config.SPIKE_MOMO_TRAIL_MFE),
            "trail_give": float(config.SPIKE_MOMO_TRAIL_GIVEBACK),
        }
    return {
        "be1_mfe": float(config.BREAKEVEN_MFE_GE),
        "be1_lock": float(config.BREAKEVEN_OFFSET_PCT),
        "be2_mfe": 99.0,
        "be2_lock": float(config.BREAKEVEN_OFFSET_PCT),
        "trail_mfe": float(config.TRAIL_ARM_MFE_GE),
        "trail_give": float(config.TRAIL_GIVEBACK_PCT),
    }


def check_exits_on_bar(
    state: PaperState,
    symbol: str,
    high: float,
    low: float,
    close: float,
    mode: str,
) -> None:
    """用該分鐘 OHLC 更新 MFE/MAE，並檢查 TP / SL / 保本鎖利 / 時間停損。"""
    pos = state.positions.get(symbol)
    if pos is None:
        return

    pos.last_price = close
    lock_before = float(pos.lock_offset_pct)
    be_before = bool(pos.breakeven_armed)
    _update_mfe_mae(pos, high=high, low=low)

    hold_mins = (time.time() - pos.entry_time) / 60.0
    cur_pct = _price_change_pct(pos, close) * 100.0
    lk = _school_lock_params(pos.school)

    # —— 門派專屬保本上移 ——
    target_lock = pos.lock_offset_pct
    if pos.max_favorable_pct >= lk["be2_mfe"]:
        target_lock = max(target_lock, lk["be2_lock"])
    if pos.max_favorable_pct >= lk["be1_mfe"]:
        target_lock = max(target_lock, lk["be1_lock"])
    lock_armed_this_bar = False
    if target_lock > pos.lock_offset_pct + 1e-12:
        pos.lock_offset_pct = target_lock
        pos.breakeven_armed = True
        lock_armed_this_bar = True
        print(
            f"{C_CYAN}  🔒 {symbol} 保本上移：MFE {pos.max_favorable_pct:.2f}% "
            f"→ SL@成本{'+' if pos.side=='LONG' else '-'}{pos.lock_offset_pct:.2f}% "
            f"（{pos.school}）{C_RESET}"
        )

    if pos.max_favorable_pct >= lk["trail_mfe"] and not pos.trail_armed:
        pos.trail_armed = True
        print(
            f"{C_CYAN}  🔐 {symbol} 鎖利啟動：MFE {pos.max_favorable_pct:.2f}%≥{lk['trail_mfe']:.2f}% "
            f"（回吐>{lk['trail_give']:.2f}% → TRAIL_PROFIT）{C_RESET}"
        )

    sl_info = effective_sl_prices(
        side=pos.side,
        entry=pos.entry_price,
        original_sl_pct=pos.original_sl_pct or pos.sl_pct,
        lock_offset_pct=pos.lock_offset_pct,
        breakeven_armed=pos.breakeven_armed,
    )

    # 回撤鎖利
    if pos.trail_armed and cur_pct <= pos.max_favorable_pct - lk["trail_give"]:
        try:
            append_trade_audit(
                {
                    "event": "MANAGE_EXIT",
                    "trade_id": pos.trade_id or "UNKNOWN",
                    "symbol": symbol,
                    "exit_trigger_type": "TRAIL_GIVEBACK_ON_CLOSE",
                    "exit_reason_label": "TRAIL_PROFIT",
                    "observed": {"high": high, "low": low, "close": close},
                    "mfe": pos.max_favorable_pct,
                    "mae": pos.max_adverse_pct,
                    "sl_before": effective_sl_prices(
                        side=pos.side,
                        entry=pos.entry_price,
                        original_sl_pct=pos.original_sl_pct or pos.sl_pct,
                        lock_offset_pct=lock_before,
                        breakeven_armed=be_before,
                    ),
                    "sl_after": sl_info,
                    "ambiguity": classify_intrabar_exit(
                        side=pos.side,
                        high=high,
                        low=low,
                        close=close,
                        tp_price=0.0,
                        sl_price=sl_info["effective_sl_price"],
                        hit_tp=False,
                        hit_sl=False,
                        lock_armed_this_bar=lock_armed_this_bar,
                        trail_exit=True,
                    ),
                    "fill_price_sim": close,
                    "note": "simulated fill at bar close; not exchange fill",
                }
            )
        except Exception:
            pass
        close_position(state, symbol, close, "TRAIL_PROFIT", mode)
        return

    # MOMO 動能停滯提早出場
    if pos.school == SCHOOL_SPIKE_MOMO:
        stall_mins = float(getattr(config, "SPIKE_MOMO_STALL_MINS", 4.0))
        stall_mfe = float(getattr(config, "SPIKE_MOMO_STALL_MFE_LT", 0.30))
        if hold_mins >= stall_mins and pos.max_favorable_pct < stall_mfe and cur_pct < 0:
            try:
                append_trade_audit(
                    {
                        "event": "MANAGE_EXIT",
                        "trade_id": pos.trade_id or "UNKNOWN",
                        "symbol": symbol,
                        "exit_trigger_type": "MOMO_STALL",
                        "exit_reason_label": "MOMO_STALL_EXIT",
                        "fill_price_sim": close,
                        "mfe": pos.max_favorable_pct,
                        "mae": pos.max_adverse_pct,
                    }
                )
            except Exception:
                pass
            close_position(state, symbol, close, "MOMO_STALL_EXIT", mode)
            return

    tp_mult = pos.tp_pct / 100.0
    sl_mult = pos.sl_pct / 100.0
    lock_mult = pos.lock_offset_pct / 100.0

    hit_tp = False
    hit_sl = False
    exit_price = close
    used_lock_sl = False
    tp_price = 0.0
    sl_price = 0.0

    if pos.side == "LONG":
        tp_price = pos.entry_price * (1 + tp_mult)
        sl_price = pos.entry_price * (1 - sl_mult)
        if pos.breakeven_armed and lock_mult > 0:
            be_price = pos.entry_price * (1 + lock_mult)
            if be_price > sl_price:
                sl_price = be_price
                used_lock_sl = True
        if high >= tp_price:
            hit_tp, exit_price = True, tp_price
        if low <= sl_price:
            if hit_tp:
                hit_tp, hit_sl, exit_price = False, True, sl_price
            else:
                hit_sl, exit_price = True, sl_price
    else:
        tp_price = pos.entry_price * (1 - tp_mult)
        sl_price = pos.entry_price * (1 + sl_mult)
        if pos.breakeven_armed and lock_mult > 0:
            be_price = pos.entry_price * (1 - lock_mult)
            if be_price < sl_price:
                sl_price = be_price
                used_lock_sl = True
        if low <= tp_price:
            hit_tp, exit_price = True, tp_price
        if high >= sl_price:
            if hit_tp:
                hit_tp, hit_sl, exit_price = False, True, sl_price
            else:
                hit_sl, exit_price = True, sl_price

    # 每分鐘持倉軌跡（即使未出場）— 用觸發前的 raw 觸碰判斷歧義
    try:
        raw_hit_tp = (high >= tp_price) if pos.side == "LONG" else (low <= tp_price)
        raw_hit_sl = (low <= sl_price) if pos.side == "LONG" else (high >= sl_price)
        both = bool(raw_hit_tp and raw_hit_sl)
        append_trade_audit(
            {
                "event": "MANAGE_TICK",
                "trade_id": pos.trade_id or "UNKNOWN",
                "symbol": symbol,
                "obs_time": _now_str(),
                "observed": {"high": high, "low": low, "close": close, "source": "closed_1m_bar"},
                "mfe": pos.max_favorable_pct,
                "mae": pos.max_adverse_pct,
                "mfe_mae_basis": "bar_ohlc_vs_entry_after_open_only",
                "breakeven_armed": pos.breakeven_armed,
                "trail_armed": pos.trail_armed,
                "lock_offset_pct": pos.lock_offset_pct,
                "lock_armed_this_bar": lock_armed_this_bar,
                "sl_levels": sl_info,
                "raw_hit_tp": raw_hit_tp,
                "raw_hit_sl": raw_hit_sl,
                "intrabar_both_tp_sl": both,
                "sim_rule_if_both": "SL_WINS",
            }
        )
    except Exception:
        pass

    if hit_sl:
        # 保本／上移停損且毛利>0 → BREAKEVEN_WIN（標籤語意；trigger 另記）
        gross, _, _, _ = _calc_pnl(pos, exit_price)
        if used_lock_sl and gross > 0:
            sl_reason = "BREAKEVEN_WIN"
            trigger = "BREAKEVEN_SL_TOUCH"
        elif used_lock_sl and gross <= 0:
            sl_reason = "SL_HIT"
            trigger = "BREAKEVEN_SL_TOUCH_NONPOSITIVE_GROSS"
        else:
            sl_reason = "SL_HIT"
            trigger = "ORIGINAL_SL_TOUCH"
        try:
            amb = classify_intrabar_exit(
                side=pos.side,
                high=high,
                low=low,
                close=close,
                tp_price=tp_price,
                sl_price=sl_price,
                hit_tp=(high >= tp_price if pos.side == "LONG" else low <= tp_price),
                hit_sl=True,
                lock_armed_this_bar=lock_armed_this_bar,
                trail_exit=False,
            )
            append_trade_audit(
                {
                    "event": "MANAGE_EXIT",
                    "trade_id": pos.trade_id or "UNKNOWN",
                    "symbol": symbol,
                    "exit_trigger_type": trigger,
                    "exit_reason_label": sl_reason,
                    "fill_price_sim": exit_price,
                    "gross_at_fill": gross,
                    "ambiguity": amb,
                    "note": "label BREAKEVEN_WIN requires gross>0; trigger is SL touch",
                }
            )
        except Exception:
            pass
        close_position(state, symbol, exit_price, sl_reason, mode)
        return
    if hit_tp:
        try:
            append_trade_audit(
                {
                    "event": "MANAGE_EXIT",
                    "trade_id": pos.trade_id or "UNKNOWN",
                    "symbol": symbol,
                    "exit_trigger_type": "HARD_TP",
                    "exit_reason_label": "TP_HIT",
                    "fill_price_sim": exit_price,
                    "ambiguity": classify_intrabar_exit(
                        side=pos.side,
                        high=high,
                        low=low,
                        close=close,
                        tp_price=tp_price,
                        sl_price=sl_price,
                        hit_tp=True,
                        hit_sl=False,
                        lock_armed_this_bar=lock_armed_this_bar,
                        trail_exit=False,
                    ),
                }
            )
        except Exception:
            pass
        close_position(state, symbol, exit_price, "TP_HIT", mode)
        return
    if hold_mins >= pos.max_hold_mins:
        try:
            append_trade_audit(
                {
                    "event": "MANAGE_EXIT",
                    "trade_id": pos.trade_id or "UNKNOWN",
                    "symbol": symbol,
                    "exit_trigger_type": "TIME_EXIT",
                    "exit_reason_label": "TIME_EXIT",
                    "fill_price_sim": close,
                }
            )
        except Exception:
            pass
        close_position(state, symbol, close, "TIME_EXIT", mode)


# ---------------------------------------------------------------------------
# 刷新榜單與換班
# ---------------------------------------------------------------------------


async def refresh_routing(
    session: aiohttp.ClientSession,
    state: PaperState,
    progress: bool = True,
) -> list[dict[str, Any]]:
    """
    重跑 Top50 解剖 + Router（預設每 10 分鐘）。
    Active Position Protection：未平倉幣種即使掉榜，仍強制保留在監控迴圈
    （標記 CLOSE_ONLY，只盯 TP/SL/TIME，絕不開新倉、不強制市價平倉）。
    """
    state.refresh_count += 1
    top_n = int(config.TOP_GAINERS) + int(config.TOP_LOSERS)
    print(f"\n{C_CYAN}{'=' * 64}{C_RESET}")
    print(
        f"{C_CYAN}🔄 第 {state.refresh_count} 次重刷 Top{top_n} 股性解剖與策略分派"
        f"（間隔 {config.REFRESH_ROUTING_MINUTES} 分鐘｜K 線增量快取）…{C_RESET}"
    )

    def _cb(cur: int, total: int, symbol: str, msg: str) -> None:
        if progress:
            print(f"  [{cur}/{total}] {msg}")

    await run_analysis(progress_callback=_cb if progress else None)
    generate_report()
    plans, plan_path = run_router()
    json_path = Path(config.ROUTING_PLAN_JSON)
    print(f"{C_CYAN}分派計畫已更新：{plan_path} / {json_path}{C_RESET}")

    new_watch: dict[str, dict[str, Any]] = {}
    tradeable_syms: set[str] = set()
    for p in plans:
        if p.get("tradeable"):
            new_watch[p["symbol"]] = p
            tradeable_syms.add(p["symbol"])

    # —— Active Position Protection ——
    # 掉榜／被剔除但仍有未平倉 → 強制保留監控（CLOSE_ONLY）
    for sym, pos in list(state.positions.items()):
        if sym not in tradeable_syms:
            pos.close_only = True
            state.close_only.add(sym)
            print(
                f"{C_YELLOW}🛡 持倉保護 {sym}：已掉出新池／非可交易，"
                f"標記 CLOSE_ONLY（每分鐘續盯 TP/SL/TIME，不開新倉、不平殺）{C_RESET}"
            )

    # 仍在可交易池者：取消 CLOSE_ONLY（可再接新訊號；同幣仍最多一倉）
    for sym in tradeable_syms:
        if sym in state.positions:
            state.positions[sym].close_only = False
            state.close_only.discard(sym)
        else:
            state.close_only.discard(sym)

    state.watchlist = new_watch
    state.last_tradeable_symbols = sorted(tradeable_syms)
    state.last_refresh_ts = time.time()
    state.last_refresh_time_str = _now_str()

    print(
        f"{C_CYAN}📋 重刷 #{state.refresh_count} 完成｜可交易池 "
        f"({len(state.last_tradeable_symbols)})：{', '.join(state.last_tradeable_symbols) or '（空）'}{C_RESET}"
    )
    if state.positions:
        print(
            f"{C_YELLOW}🛡 持倉保護中：{', '.join(sorted(state.positions.keys()))} "
            f"（CLOSE_ONLY={', '.join(sorted(state.close_only)) or '無'}）{C_RESET}"
        )
    write_daily_review(state)
    # B 區：Top15 活躍妖幣 K 線快照（失敗不影響主迴圈）
    try:
        update_zone_b_and_write(plans, note=f"重刷#{state.refresh_count}")
        print(f"{C_DIM}K線切片已更新：{config.KLINE_SLICES_FOR_AI_MD}{C_RESET}")
    except Exception as exc:
        print(f"{C_DIM}  （K線快照略過：{exc}）{C_RESET}")
    # 決策：榜單刷新池快照（不改分流結果）
    try:
        append_decision_trace(
            {
                "event": "ROUTING_REFRESH",
                "decision_time": _now_str(),
                "refresh_count": state.refresh_count,
                "candidate_pool": [
                    {
                        "symbol": p.get("symbol"),
                        "status": p.get("status"),
                        "primary_school": p.get("primary_school"),
                        "tradeable": bool(p.get("tradeable")),
                        "skip_reason": p.get("skip_reason"),
                        "price_change_pct_24h": p.get("price_change_pct_24h"),
                    }
                    for p in plans
                ],
                "tradeable_pool": sorted(tradeable_syms),
                "note": "区分：未进可交易池 vs 扫描到但无交易",
            }
        )
    except Exception:
        pass
    return plans


# ---------------------------------------------------------------------------
# 單輪掃描
# ---------------------------------------------------------------------------


async def scan_once(
    session: aiohttp.ClientSession,
    state: PaperState,
    mode: str = "once",
    check_15m: bool = True,
) -> None:
    """抓最新 K 線、檢查出場與進場訊號、印監控面板。"""
    symbols = set(state.watchlist.keys()) | set(state.positions.keys())
    if not symbols:
        print(f"{C_YELLOW}監控池為空（無可交易門派）。{C_RESET}")
        print_wallet_status(state)
        return

    print(
        f"\n{C_BOLD}📡 訊號掃描 {_now_str()}｜監控 {len(symbols)} 幣"
        f"｜重刷次數 {state.refresh_count}{C_RESET}"
    )
    if state.last_tradeable_symbols:
        print(f"{C_DIM}可交易池：{', '.join(state.last_tradeable_symbols)}{C_RESET}")
    print_wallet_status(state)

    for symbol in sorted(symbols):
        try:
            plan = state.watchlist.get(symbol)
            schools: list[dict[str, Any]] = list((plan or {}).get("schools") or [])
            holding = symbol in state.positions
            primary_for_fetch = str((plan or {}).get("primary_school") or "")

            # 只為主門派預抓 15m（不再為備援門派輪流抓）
            need_15m = (not holding) and (
                (
                    check_15m
                    and primary_for_fetch in (SCHOOL_MEAN_REV, SCHOOL_TREND)
                )
                or primary_for_fetch == SCHOOL_SWING_REV  # SWING 每分鐘需 15m 閘門
            )

            df_1m = await fetch_klines_df(session, symbol, "1m", lookback_days=1)
            df_15m = (
                await fetch_klines_df(session, symbol, "15m", lookback_days=2)
                if need_15m
                else pd.DataFrame()
            )

            closed_1m = _closed_klines(df_1m, "1m")
            if not closed_1m.empty:
                last = closed_1m.iloc[-1]
                check_exits_on_bar(
                    state,
                    symbol,
                    high=float(last["high"]),
                    low=float(last["low"]),
                    close=float(last["close"]),
                    mode=mode,
                )

            # 已有倉（含 CLOSE_ONLY）：只盯出場，不再開新倉
            if symbol in state.positions:
                pos = state.positions[symbol]
                tag = "CLOSE_ONLY" if pos.close_only else "HOLD"
                print(
                    f"  {C_DIM}[{tag}] {symbol} {pos.side} @{pos.entry_price:.8g} "
                    f"TP{pos.tp_pct:.2f}% SL{pos.sl_pct:.2f}% school={pos.school}{C_RESET}"
                )
                continue

            if not plan or not plan.get("tradeable"):
                try:
                    append_decision_trace(
                        {
                            "event": "SIGNAL_EVAL",
                            "decision_time": _now_str(),
                            "symbol": symbol,
                            "in_scan_pool": True,
                            "in_tradeable_pool": False,
                            "signal_status": "NOT_EVALUATED",
                            "final_action": "BLOCKED",
                            "block_reason": "NOT_TRADEABLE_OR_WATCH_SKIP",
                        }
                    )
                except Exception:
                    pass
                continue

            # 只准使用該幣被分派到的主門派（primary_school），禁止輪流測所有門派
            primary = str(plan.get("primary_school") or "")
            match = next(
                (s for s in schools if s.get("school") == primary),
                None,
            )
            if match is None and schools:
                # 防禦：若 primary 缺失則退回第一個（不應發生）
                match = schools[0]
                primary = str(match.get("school") or "")
            if not match or not primary:
                print(f"  ○ {symbol}｜無主門派分派")
                continue

            school = primary
            if school in (SCHOOL_MEAN_REV, SCHOOL_TREND) and not check_15m:
                print(f"  ○ {symbol}｜{school}｜本輪略過 15m 門派掃描")
                continue
            # MEAN/TREND/SWING 皆需 15m；SWING 每分鐘都做 15m 閘門
            if school in (SCHOOL_MEAN_REV, SCHOOL_TREND, SCHOOL_SWING_REV) and df_15m.empty:
                df_15m = await fetch_klines_df(session, symbol, "15m", lookback_days=2)

            sig_ctx = {
                "price_position_pct": plan.get("price_position_pct", 50.0),
                "price_change_pct_24h": plan.get("price_change_pct_24h", 0.0),
                "change_24h_pct": plan.get("price_change_pct_24h", 0.0),
            }
            # 決策紀錄：只記錄實際評估的主門派（不輪流下單）
            decision: dict[str, Any] = {
                "event": "SIGNAL_EVAL",
                "decision_time": _now_str(),
                "symbol": symbol,
                "in_scan_pool": True,
                "in_tradeable_pool": True,
                "personality_snapshot": {
                    "price_position_pct": plan.get("price_position_pct"),
                    "price_change_pct_24h": plan.get("price_change_pct_24h"),
                    "recent_volume_ratio": plan.get("recent_volume_ratio"),
                    "snapshot_time": state.last_refresh_time_str,
                    "freshness": "from_last_routing_refresh",
                },
                "primary_school": primary,
                "allowed_schools": [s.get("school") for s in schools],
                "evaluated_school": school,
                "other_schools": "NOT_EVALUATED",
                "signal_status": "NOT_EVALUATED",
                "checks": [],
                "final_action": "NO_ENTRY",
                "block_reason": None,
            }
            if state.is_in_sl_cooldown(symbol):
                decision["signal_status"] = "REJECT"
                decision["final_action"] = "BLOCKED"
                decision["block_reason"] = "SL_COOLDOWN"
                try:
                    append_decision_trace(decision)
                except Exception:
                    pass
                rem = max(0.0, state.sl_cooldown_until.get(symbol, 0.0) - time.time())
                print(
                    f"  ○ {symbol}｜SL 冷卻中"
                    f"（剩餘 {rem/60.0:.1f}m，至 {_fmt_cooldown_until(state, symbol)}）"
                )
                continue

            if len(state.positions) >= config.MAX_CONCURRENT_POSITIONS:
                decision["signal_status"] = "REJECT"
                decision["final_action"] = "BLOCKED"
                decision["block_reason"] = "MAX_POSITIONS"
                # 仍可評估訊號以便復盤，但不下單
            if symbol in state.positions:
                decision["signal_status"] = "REJECT"
                decision["final_action"] = "BLOCKED"
                decision["block_reason"] = "SAME_SYMBOL_POSITION"
                try:
                    append_decision_trace(decision)
                except Exception:
                    pass
                continue

            # 旁路診斷：begin → detect → take；失敗不影響決策
            probe_payload = None
            try:
                begin_probe(
                    school=school,
                    symbol=symbol,
                    scan_time=decision["decision_time"],
                    ctx=sig_ctx,
                    extra={
                        "routing_recent_volume_ratio": plan.get("recent_volume_ratio"),
                    },
                )
            except Exception as exc:
                warn_write_error("begin_probe", exc)
            try:
                sig = detect_signal_for_school(school, df_1m, df_15m, ctx=sig_ctx)
            finally:
                try:
                    probe_payload = take_probe()
                except Exception as exc:
                    probe_payload = None
                    warn_write_error("take_probe", exc)
            if probe_payload is not None:
                decision["signal_reject"] = {
                    "first_reject_code": probe_payload.get("first_reject_code"),
                    "result": probe_payload.get("result"),
                    "signal_bar_open_time": probe_payload.get("signal_bar_open_time"),
                    "signal_bar_open_time_iso": probe_payload.get("signal_bar_open_time_iso"),
                    "input_last_bar_open_time": probe_payload.get("input_last_bar_open_time"),
                    "input_last_bar_close_time": probe_payload.get("input_last_bar_close_time"),
                    "bars_before_closed_filter": probe_payload.get("bars_before_closed_filter"),
                    "bars_after_closed_filter": probe_payload.get("bars_after_closed_filter"),
                    "excluded_unclosed_open_times": probe_payload.get("excluded_unclosed_open_times"),
                    "vol_metric": probe_payload.get("vol_metric"),
                    "conditions": probe_payload.get("conditions"),
                    "routing_vol_ratio_note": probe_payload.get("routing_vol_ratio_note"),
                }
                try:
                    append_reject_diag_jsonl(
                        {
                            "source": "live_scan",
                            "run_id": get_run_id(),
                            **probe_payload,
                        }
                    )
                except Exception as exc:
                    warn_write_error("append_reject_diag_jsonl", exc)
            if not sig:
                decision["signal_status"] = "REJECT"
                decision["final_action"] = "NO_ENTRY"
                decision["block_reason"] = "NO_SIGNAL"
                fr = (probe_payload or {}).get("first_reject_code") if probe_payload else None
                decision["checks"] = [
                    {
                        "name": "detect_signal_for_school",
                        "school": school,
                        "result": "FAIL",
                        "first_reject_code": fr,
                        "note": (
                            f"first_reject={fr}; later conditions NOT_EVALUATED "
                            "(order-dependent, not independent fail rates)"
                            if fr
                            else "internal thresholds short-circuit; unevaluated subchecks=NOT_EVALUATED"
                        ),
                    }
                ]
                try:
                    append_decision_trace(decision)
                except Exception as exc:
                    warn_write_error("append_decision_trace", exc)
                print(f"  ○ {symbol}｜{school}｜無訊號" + (f"｜{fr}" if fr else ""))
                continue
            decision["signal_status"] = "PASS"
            decision["evaluated_side"] = sig.get("side")
            decision["checks"] = [
                {
                    "name": "detect_signal_for_school",
                    "school": school,
                    "result": "PASS",
                    "side": sig.get("side"),
                    "detail": sig.get("detail"),
                }
            ]
            sig_id = f"{symbol}:{sig['signal_key']}"
            if sig_id in state.last_signal_keys:
                decision["final_action"] = "BLOCKED"
                decision["block_reason"] = "SIGNAL_ALREADY_HANDLED"
                try:
                    append_decision_trace(decision)
                except Exception:
                    pass
                print(f"  ○ {symbol}｜{school}｜訊號已處理過（防重）")
                continue
            if decision.get("block_reason") == "MAX_POSITIONS":
                try:
                    append_decision_trace(decision)
                except Exception:
                    pass
                print(f"  ○ {symbol}｜{school}｜訊號PASS但持倉額滿")
                continue

            state.last_signal_keys.add(sig_id)
            if len(state.last_signal_keys) > 5000:
                state.last_signal_keys = set(list(state.last_signal_keys)[-2000:])

            opened = open_position(
                state,
                symbol=symbol,
                school=school,
                side=sig["side"],
                entry=float(sig["entry"]),
                params=match["params"],
                reason=sig["detail"],
                mode=mode,
                df_1m=df_1m,
                df_15m=df_15m,
                plan=plan,
            )
            if opened is None:
                decision["final_action"] = "BLOCKED"
                decision["block_reason"] = "OPEN_REJECTED"
            else:
                decision["final_action"] = "OPEN"
                decision["block_reason"] = None
                decision["trade_id"] = opened.trade_id
            try:
                append_decision_trace(decision)
            except Exception:
                pass
        except Exception as exc:
            print(f"{C_RED}  ✗ {symbol} 掃描失敗：{exc}{C_RESET}")
            try:
                append_decision_trace(
                    {
                        "event": "SIGNAL_EVAL",
                        "decision_time": _now_str(),
                        "symbol": symbol,
                        "signal_status": "DATA_INVALID",
                        "final_action": "BLOCKED",
                        "block_reason": f"EXCEPTION:{exc}",
                    }
                )
            except Exception:
                pass

    # 面板摘要
    print(f"\n{C_BOLD}—— 監控面板 ——{C_RESET}")
    print_wallet_status(state)
    print(f"可交易池：{', '.join(sorted(state.watchlist.keys())) or '（空）'}")
    if state.close_only:
        print(f"CLOSE_ONLY：{', '.join(sorted(state.close_only))}")
    if state.positions:
        for p in state.positions.values():
            mark = p.last_price if p.last_price > 0 else p.entry_price
            _, _, net, roi = _calc_pnl(p, mark)
            print(
                f"  持倉 {p.symbol} {p.side} entry={p.entry_price:.8g} mark={mark:.8g} "
                f"浮動淨損益 {net:+.4f}U ROI {roi:+.2f}% "
                f"TP={p.tp_pct:.2f}% SL={p.sl_pct:.2f}% hold≤{p.max_hold_mins}m"
            )
    else:
        print("  目前無持倉")
    print(f"複盤報告：{config.DAILY_REVIEW_MD}")
    write_daily_review(state)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def run_paper_trader(loop_mode: bool = False) -> None:
    state = PaperState()

    # 稽核輸出初始化（FADE long-only 為決策變更 → 新 strategy_version／run_id；舊 manifest 歸檔）
    try:
        paths = bootstrap_audit_outputs()
        ensure_run_manifest_for_current_strategy()
        print(
            f"{C_CYAN}🧾 稽核輸出：run_id={paths.get('run_id')}｜"
            f"strategy_version={paths.get('strategy_version')}｜"
            f"prev={paths.get('previous_run_id')}｜"
            f"{paths.get('execution_audit')}｜{paths.get('ai_review_bundle')}{C_RESET}"
        )
    except Exception as exc:
        print(f"{C_DIM}稽核初始化略過：{exc}{C_RESET}")

    # 先從既有 CSV 恢復已實現損益與歷史（絕不洗掉戰績）
    restored = restore_state_from_trades_log(state)
    cd_n = int(restored.get("sl_cooldowns", 0) or 0)
    print(
        f"{C_CYAN}📂 已載入歷史戰績：平倉 {restored.get('closed', 0)} 筆｜"
        f"已實現淨損益 {restored.get('realized_net', 0.0):+.4f} U｜"
        f"累積手續費 {restored.get('realized_fees', 0.0):.4f} U"
        f"{f'｜SL 冷卻中 {cd_n} 幣' if cd_n else ''}{C_RESET}"
    )
    write_daily_review(state)
    print_wallet_status(state)

    timeout = aiohttp.ClientTimeout(total=120)
    connector = aiohttp.TCPConnector(limit=8)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # 若尚無股性 CSV，或 loop 模式啟動時，先刷一輪
        summary = Path(config.SUMMARY_CSV)
        if not summary.exists() or loop_mode:
            await refresh_routing(session, state, progress=True)
        else:
            plans, plan_path = run_router()
            for p in plans:
                if p.get("tradeable"):
                    state.watchlist[p["symbol"]] = p
            state.last_refresh_ts = time.time()
            print(f"已載入分派計畫：{plan_path}｜可交易 {len(state.watchlist)} 支")

        if not loop_mode:
            # --once：15m 訊號也一併檢查（驗證用）
            await scan_once(session, state, mode="once", check_15m=True)
            write_daily_review(state)
            print(f"\n{C_GREEN}--once 掃描完成。{C_RESET}")
            return

        print(f"\n{C_GREEN}🚀 進入 24h 循環盯盤（Ctrl+C 結束）{C_RESET}")
        print(
            f"每 {config.REFRESH_ROUTING_MINUTES} 分鐘重刷榜單；"
            f"每分鐘第 {config.SIGNAL_CHECK_SECOND} 秒掃描已收盤 K 線"
        )
        print_wallet_status(state)

        while True:
            try:
                now = time.time()
                # 每小時重刷
                if now - state.last_refresh_ts >= config.REFRESH_ROUTING_MINUTES * 60:
                    await refresh_routing(session, state, progress=True)

                # 對齊到每分鐘的第 N 秒
                local = datetime.now().astimezone()
                target = local.replace(second=config.SIGNAL_CHECK_SECOND, microsecond=0)
                if local.second > config.SIGNAL_CHECK_SECOND or (
                    local.second == config.SIGNAL_CHECK_SECOND and local.microsecond > 0
                ):
                    # 下一分鐘
                    target = datetime.fromtimestamp(target.timestamp() + 60, tz=target.tzinfo)
                    target = target.replace(second=config.SIGNAL_CHECK_SECOND, microsecond=0)
                wait = max(0.0, target.timestamp() - time.time())
                await asyncio.sleep(wait)

                minute = datetime.now().astimezone().minute
                check_15m = minute % 15 == 0
                await scan_once(session, state, mode="loop", check_15m=check_15m)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"{C_RED}循環錯誤：{exc}（5 秒後重試）{C_RESET}")
                await asyncio.sleep(5)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="幣安公開 API 模擬盯盤")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="單次掃描後結束（預設）")
    group.add_argument("--loop", action="store_true", help="24h 循環盯盤")
    args = parser.parse_args(argv)
    loop_mode = bool(args.loop)
    try:
        asyncio.run(run_paper_trader(loop_mode=loop_mode))
    except KeyboardInterrupt:
        print("\n已停止模擬盯盤。複盤報告已保留於 output/daily_review_for_ai.md")


if __name__ == "__main__":
    main()
