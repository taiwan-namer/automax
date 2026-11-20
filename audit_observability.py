"""
交易可信度稽核與 AI 復盤輸出（不改策略決策）

產物：
  - output/execution_audit.md
  - output/run_manifest.json
  - output/decision_trace_YYYYMMDD.jsonl
  - output/trade_audit_YYYYMMDD.jsonl
  - output/ai_review_bundle.md
  - output/kline_archives/{trade_id}.json.gz
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

import config

STRATEGY_VERSION = "fade_long_only_v2"
TZ_NAME = "Asia/Taipei"
DECISION_CHANGE_NOTES = [
    "SPIKE_FADE_1M long-only: detect never returns SHORT; reject code FADE_SHORT_DISABLED",
    "open_position defense gate rejects school=SPIKE_FADE_1M side=SHORT before margin/OPEN",
    "This version intentionally changes entry decisions vs audit_obs_v1; not a no-op observability bump",
]

# ---------------------------------------------------------------------------
# 路徑
# ---------------------------------------------------------------------------

def _out(name: str) -> Path:
    return Path("output") / name


def _today_tag() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_git_commit() -> str:
    try:
        import subprocess

        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "UNKNOWN"


def config_hash() -> str:
    """對關鍵設定做穩定 hash（不含路徑類變動欄位亦可）。"""
    keys = sorted(
        k
        for k in dir(config)
        if k.isupper() and not k.startswith("_")
    )
    payload = {k: getattr(config, k) for k in keys}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def school_params_snapshot() -> dict[str, Any]:
    return {
        "MEAN_REV_15M": {
            "enabled": True,
            "tp": config.MEAN_REV_TP_PCT,
            "sl": config.MEAN_REV_SL_PCT,
            "max_hold": config.MEAN_REV_MAX_HOLD_MINS,
        },
        "SPIKE_FADE_1M": {
            "enabled": True,
            "short_enabled": False,
            "long_only": True,
            "tp_range": [config.SPIKE_FADE_TP_MIN, config.SPIKE_FADE_TP_MAX],
            "sl_range": [config.SPIKE_FADE_SL_MIN, config.SPIKE_FADE_SL_MAX],
            "max_hold": config.SPIKE_FADE_MAX_HOLD_MINS,
            "vol_x": [config.SPIKE_FADE_VOL_X_MIN, config.SPIKE_FADE_VOL_X_MAX],
            "wick_min": config.SPIKE_FADE_WICK_MIN,
        },
        "SPIKE_MOMO_1M": {
            "enabled": True,
            "tp": config.SPIKE_MOMO_TP_PCT,
            "sl": config.SPIKE_MOMO_SL_PCT,
            "max_hold": config.SPIKE_MOMO_MAX_HOLD_MINS,
            "vol_x": [config.SPIKE_MOMO_VOL_X_MIN, config.SPIKE_MOMO_VOL_X_MAX],
            "body_min": config.SPIKE_MOMO_BODY_MIN,
        },
        "TREND_FOLLOW_15M": {
            "enabled": True,
            "tp": config.TREND_TP_PCT,
            "sl": config.TREND_SL_PCT,
            "max_hold": config.TREND_MAX_HOLD_MINS,
        },
        "SWING_REV_15M": {
            "enabled": bool(getattr(config, "SWING_REV_ENABLED", False)),
            "tp": config.SWING_TP_PCT,
            "sl": config.SWING_SL_PCT,
            "max_hold": config.SWING_MAX_HOLD_MINS,
        },
    }


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------

_CURRENT_RUN: dict[str, Any] = {}


def get_run_id() -> str:
    return str(_CURRENT_RUN.get("run_id") or "LEGACY_UNKNOWN")


def get_strategy_version() -> str:
    return str(_CURRENT_RUN.get("strategy_version") or STRATEGY_VERSION)


def _archive_run_manifest(data: dict[str, Any]) -> Optional[Path]:
    """保留舊 manifest 證據：複製到 archive，不刪除歷史內容語意。"""
    try:
        run_id = str(data.get("run_id") or "UNKNOWN")
        arch_dir = _out("run_manifest_archive")
        arch_dir.mkdir(parents=True, exist_ok=True)
        dest = arch_dir / f"{run_id}.json"
        if not dest.exists():
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return dest
    except Exception:
        return None


def init_or_load_run_manifest(force_new: bool = False) -> dict[str, Any]:
    global _CURRENT_RUN
    path = _out("run_manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force_new:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("run_id"):
                _CURRENT_RUN = data
                return data
        except Exception:
            pass

    prev_run_id = None
    prev_version = None
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                prev_run_id = old.get("run_id")
                prev_version = old.get("strategy_version")
                _archive_run_manifest(old)
        except Exception:
            pass

    run_id = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    notes = [
        "historical trades before this run_id are LEGACY_UNKNOWN",
        "capital may have been raised 100→500 without ledger adjustment record",
        *DECISION_CHANGE_NOTES,
    ]
    if prev_run_id:
        notes.append(f"supersedes_archived_run_id={prev_run_id} prev_strategy_version={prev_version}")
        notes.append("old manifest archived under output/run_manifest_archive/ (not overwritten in-place as sole copy)")
    data = {
        "run_id": run_id,
        "strategy_version": STRATEGY_VERSION,
        "config_hash": config_hash(),
        "git_commit": _safe_git_commit(),
        "effective_from": _now_iso(),
        "timezone": TZ_NAME,
        "previous_run_id": prev_run_id,
        "previous_strategy_version": prev_version,
        "capital": {
            "initial_capital_usdt": config.INITIAL_CAPITAL_USDT,
            "leverage": config.LEVERAGE,
            "margin_per_trade": config.MARGIN_PER_TRADE_USDT,
            "notional": config.NOTIONAL_USDT,
            "max_positions": config.MAX_CONCURRENT_POSITIONS,
        },
        "cost_model": {
            "round_trip_fee_rate": config.ROUND_TRIP_FEE_RATE,
            "entry_fee_separate": False,
            "exit_fee_separate": False,
            "fee_charged_at": "CLOSE_ONLY_ROUND_TRIP",
            "slippage_model": "NONE",
        },
        "routing": {
            "refresh_minutes": config.REFRESH_ROUTING_MINUTES,
            "top_gainers": config.TOP_GAINERS,
            "top_losers": config.TOP_LOSERS,
            "swing_enabled": bool(getattr(config, "SWING_REV_ENABLED", False)),
            "school_priority": [
                "MEAN_REV_15M",
                "SPIKE_FADE_1M",
                "SPIKE_MOMO_1M",
                "TREND_FOLLOW_15M",
                "SWING_REV_15M",
            ],
            "primary_only_scan": True,
        },
        "schools": school_params_snapshot(),
        "signal_check_second": config.SIGNAL_CHECK_SECOND,
        "poll_interval": "1m",
        "notes": notes,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _CURRENT_RUN = data
    return data


def ensure_run_manifest_for_current_strategy() -> dict[str, Any]:
    """
    若現有 manifest 的 strategy_version 與程式不同（決策變更），
    先歸檔舊檔再建立新 run_id；否則沿用。
    """
    path = _out("run_manifest.json")
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and old.get("strategy_version") == STRATEGY_VERSION and old.get("run_id"):
                return init_or_load_run_manifest(force_new=False)
        except Exception:
            pass
        # 版本不一致或損壞 → 強制新 run（內部會 archive）
        return init_or_load_run_manifest(force_new=True)
    return init_or_load_run_manifest(force_new=True)


# ---------------------------------------------------------------------------
# JSONL appenders
# ---------------------------------------------------------------------------

def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        # 不得靜默吞錯；節流提示；不拋回交易路徑
        try:
            from signal_reject_diag import warn_write_error

            warn_write_error(str(path), exc)
        except Exception:
            print(f"[diag-write-warn] {path}: {type(exc).__name__}: {exc}")


def append_decision_trace(record: dict[str, Any]) -> None:
    rec = {
        "run_id": get_run_id(),
        "strategy_version": get_strategy_version(),
        "recorded_at": _now_iso(),
        **record,
    }
    _append_jsonl(_out(f"decision_trace_{_today_tag()}.jsonl"), rec)


def append_trade_audit(record: dict[str, Any]) -> None:
    rec = {
        "run_id": get_run_id(),
        "strategy_version": get_strategy_version(),
        "recorded_at": _now_iso(),
        **record,
    }
    _append_jsonl(_out(f"trade_audit_{_today_tag()}.jsonl"), rec)


# ---------------------------------------------------------------------------
# 出場歧義標記（不改成交規則，只標記）
# ---------------------------------------------------------------------------

def classify_intrabar_exit(
    *,
    side: str,
    high: float,
    low: float,
    close: float,
    tp_price: float,
    sl_price: float,
    hit_tp: bool,
    hit_sl: bool,
    lock_armed_this_bar: bool,
    trail_exit: bool,
) -> dict[str, Any]:
    """
    依目前模擬規則描述同根歧義；不覆寫歷史成交。
    """
    both = bool(hit_tp and hit_sl)
    tags: list[str] = []
    if both:
        tags.append("AMBIGUOUS_INTRABAR")
        tags.append("SIM_RULE_SL_OVER_TP")
    if lock_armed_this_bar and hit_sl:
        tags.append("AMBIGUOUS_INTRABAR_ARM_AND_STOP")
    if trail_exit:
        tags.append("TRAIL_USES_CLOSE_NOT_PATH")

    # 樂觀／保守邊界（僅標記，不改歷史）
    optimistic_exit = None
    conservative_exit = None
    if both:
        # 樂觀：假設先碰到有利端 → TP
        optimistic_exit = float(tp_price)
        # 保守：目前模擬＝SL 優先
        conservative_exit = float(sl_price)
    elif hit_sl:
        conservative_exit = float(sl_price)
        optimistic_exit = float(sl_price)
    elif hit_tp:
        optimistic_exit = float(tp_price)
        conservative_exit = float(tp_price)

    return {
        "intrabar_ambiguity": tags[0] if tags else "NONE",
        "ambiguity_tags": tags,
        "sim_exit_assumption": "SL_WINS_IF_BOTH" if both else ("SL" if hit_sl else ("TP" if hit_tp else "OTHER")),
        "simulated_fill_price": "SEE_EXIT_PRICE",
        "observed_bar": {"high": high, "low": low, "close": close},
        "optimistic_boundary_price": optimistic_exit,
        "conservative_boundary_price": conservative_exit,
        "path_certainty": "UNKNOWN_INTRABAR" if (both or lock_armed_this_bar) else "BAR_CLOSE_ONLY",
    }


def effective_sl_prices(
    *,
    side: str,
    entry: float,
    original_sl_pct: float,
    lock_offset_pct: float,
    breakeven_armed: bool,
) -> dict[str, Any]:
    """區分原始 SL / 當前有效 SL / 保本停損。"""
    if side == "LONG":
        original = entry * (1 - original_sl_pct / 100.0)
        be = entry * (1 + lock_offset_pct / 100.0) if breakeven_armed and lock_offset_pct > 0 else None
        effective = original
        if be is not None and be > effective:
            effective = be
        # 收緊＝對 LONG 為提高 SL 價
        tightened = effective >= original - 1e-12
    else:
        original = entry * (1 + original_sl_pct / 100.0)
        be = entry * (1 - lock_offset_pct / 100.0) if breakeven_armed and lock_offset_pct > 0 else None
        effective = original
        if be is not None and be < effective:
            effective = be
        tightened = effective <= original + 1e-12
    return {
        "original_sl_price": original,
        "breakeven_sl_price": be,
        "effective_sl_price": effective,
        "only_tightens": tightened,
        "tp_role": "HARD_EXIT_TARGET",
        "trail_role": "SOFT_EXIT_ON_GIVEBACK_FROM_MFE_USING_CLOSE",
    }


# ---------------------------------------------------------------------------
# K 線歸檔（完整，不受 MD 截斷）
# ---------------------------------------------------------------------------

def archive_trade_klines(
    trade_id: str,
    *,
    symbol: str,
    pre_1m: Optional[pd.DataFrame] = None,
    pre_15m: Optional[pd.DataFrame] = None,
    hold_1m: Optional[pd.DataFrame] = None,
    post_1m: Optional[pd.DataFrame] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Path:
    arch_dir = _out("kline_archives")
    arch_dir.mkdir(parents=True, exist_ok=True)
    path = arch_dir / f"{trade_id.replace(':', '_')}.json.gz"

    def _recs(df: Optional[pd.DataFrame]) -> list[dict[str, Any]]:
        if df is None or getattr(df, "empty", True):
            return []
        cols = [c for c in ("open_time", "open", "high", "low", "close", "volume") if c in df.columns]
        try:
            return df[cols].to_dict(orient="records")
        except Exception:
            return []

    payload = {
        "trade_id": trade_id,
        "symbol": symbol,
        "archived_at": _now_iso(),
        "meta": meta or {},
        "pre_1m": _recs(pre_1m),
        "pre_15m": _recs(pre_15m),
        "hold_1m": _recs(hold_1m),
        "post_1m": _recs(post_1m) if post_1m is not None else "PENDING",
        "note": "post_1m for review only; never used as entry features",
    }
    try:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
    except Exception:
        pass
    return path


# ---------------------------------------------------------------------------
# execution_audit.md（靜態路徑稽核 + 歷史帳務抽樣）
# ---------------------------------------------------------------------------

def write_execution_audit_md() -> Path:
    path = _out("execution_audit.md")
    lines: list[str] = []
    lines.append("# 成交與帳務可信度稽核（execution_audit）")
    lines.append("")
    lines.append(f"> 產生時間：{_now_iso()}（{TZ_NAME}）")
    lines.append(f"> strategy_version：`{STRATEGY_VERSION}`｜config_hash：`{config_hash()}`｜git：`{_safe_git_commit()}`")
    lines.append("")
    lines.append("判定等級：`PASS` / `FAIL` / `UNKNOWN`")
    lines.append("問題類型：`程式缺陷` / `模擬限制` / `缺少紀錄`")
    lines.append("")
    lines.append("---")
    lines.append("")

    # A
    lines.append("## A. 多空保本與移動停損")
    lines.append("")
    lines.append("| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        "| LONG 保利停損上移 | PASS | `live_paper_trader.py` / `check_exits_on_bar` | "
        "`be_price = entry*(1+lock%)`，且僅在 `be_price > original_sl` 時取代 | — |"
    )
    lines.append(
        "| SHORT 保利停損下移 | PASS | 同上 | "
        "`be_price = entry*(1-lock%)`，且僅在 `be_price < original_sl` 時取代 | — |"
    )
    lines.append(
        "| 停損只能收緊 | PASS | `effective_sl_prices` / `check_exits_on_bar` | "
        "LONG 有效SL=max(原SL,保本)；SHORT=min(原SL,保本)；`lock_offset` 只增不減 | — |"
    )
    lines.append(
        "| 區分原始/有效/保本/追蹤 | PASS（紀錄層新增） | `audit_observability.effective_sl_prices` | "
        "原模擬只保存最終出場價；新 trade_audit 分開記錄。歷史舊單為 UNKNOWN | 缺少紀錄（歷史） |"
    )
    lines.append(
        "| TP 角色 | PASS | `check_exits_on_bar` | TP 為硬出場目標（觸及即 `TP_HIT`）；"
        "追蹤鎖利另以 MFE 回吐用 **close** 觸發 `TRAIL_PROFIT`，不是把 TP 當追蹤門檻 | — |"
    )
    lines.append(
        "| BREAKEVEN_WIN 標籤依據 | FAIL（標籤語意） | `check_exits_on_bar` L1515-1524 | "
        "觸發機制是「有效保本SL被碰到」；但標籤額外要求 `gross>0` 才叫 BREAKEVEN_WIN，"
        "否則仍記 SL_HIT。**不可**只因 gross>0 就當成追蹤停利；"
        "目前也**沒有**把 TRAIL 與 BE 混稱。建議：以 `exit_trigger_type=BREAKEVEN_SL` 為準，"
        "gross 僅作附註。最小修補：新增 trigger 欄，不改成交價。 | 程式缺陷（標籤） |"
    )
    lines.append("")

    # B
    lines.append("## B. 時間與 K 線邊界")
    lines.append("")
    lines.append("| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        "| 訊號只用已收盤K | PASS | `_closed_klines` + 各 `detect_*` | "
        "`open_time + interval_ms <= now` 才納入 | — |"
    )
    lines.append(
        "| 進場前 high/low 是否算入持倉路徑 | PASS（同輪） | `scan_once` | "
        "先 `check_exits` 再 `open_position`；新倉同輪不再用該根 OHLC 做出場。"
        "下一根才開始 MFE/出場 | — |"
    )
    lines.append(
        "| 出場後同根波動是否算入 MFE | PASS | `close_position` → del position | "
        "平倉後移除持倉，後續 bar 不再更新該倉 MFE | — |"
    )
    lines.append(
        "| 分鐘粒度路徑精確性 | UNKNOWN | `check_exits_on_bar` | "
        "僅有 OHLC，無法還原持倉內真實價格路徑；標示為模擬假設 | 模擬限制 |"
    )
    lines.append("")

    # C
    lines.append("## C. 同根 K 價格先後順序")
    lines.append("")
    lines.append("| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        "| 同根同碰 TP+SL 規則 | PASS（規則明確）／模擬限制 | `check_exits_on_bar` | "
        "若兩者皆觸發，**強制 SL 勝出**（`hit_tp=False, hit_sl=True`）。"
        "無開高低收路徑推論。標記建議：`AMBIGUOUS_INTRABAR` | 模擬限制 |"
    )
    lines.append(
        "| 同根先用高低更新保本再觸發 | FAIL（可信度風險） | `check_exits_on_bar` | "
        "同一函式內先 `_update_mfe_mae(high,low)` 並可能上移 lock，"
        "再立刻用同根另一端觸發有效SL。若真實路徑是先碰到低點再高點，結果可能不同。"
        "應標記 `AMBIGUOUS_INTRABAR_ARM_AND_STOP`；**不建議未確認就改規則** | 模擬限制／潛在程式缺陷 |"
    )
    lines.append(
        "| 樂觀/保守邊界 | UNKNOWN（歷史）／PASS（新單可記） | `classify_intrabar_exit` | "
        "歷史成交未存邊界；新 trade_audit 可寫 optimistic/conservative。"
        "不覆寫歷史模擬結果 | 缺少紀錄（歷史） |"
    )
    lines.append(
        "| 模擬成交價 vs 觀測價 | PASS（需分稱） | close / tp_price / sl_price | "
        "TP/SL 觸發用公式價；TRAIL/TIME/MOMO_STALL 多用 **bar close**。"
        "皆為模擬假設成交，非交易所實際成交 | 模擬限制 |"
    )
    lines.append("")

    # D
    lines.append("## D. 帳務")
    lines.append("")
    fee_note = "UNKNOWN"
    capital_note = "UNKNOWN"
    exit_dist = {}
    try:
        df = pd.read_csv(_out("live_trades_log.csv"))
        closed = df[df["event"].astype(str).str.upper() == "CLOSE"].copy()
        opens = df[df["event"].astype(str).str.upper() == "OPEN"].copy()
        exit_dist = closed["exit_reason"].fillna("-").astype(str).value_counts().to_dict()
        n_close = len(closed)
        n_reason = int(closed["exit_reason"].notna().sum())
        fee_sum = float(pd.to_numeric(closed["fee_u"], errors="coerce").fillna(0).sum())
        bal = pd.to_numeric(df["balance_after_u"], errors="coerce")
        bal_min, bal_max = float(bal.min()), float(bal.max())
        net_sum = float(pd.to_numeric(closed["net_pnl_u"], errors="coerce").fillna(0).sum())
        fee_note = (
            f"平倉 {n_close} 筆；exit_reason 合計 {n_reason}；"
            f"fee_u 總和 {fee_sum:.4f}；OPEN 不寫 fee；"
            f"_calc_pnl 以 notional×{config.ROUND_TRIP_FEE_RATE} **一次**計雙邊費"
        )
        capital_note = (
            f"balance_after_u 範圍 {bal_min:.4f}~{bal_max:.4f}；"
            f"closed net 加總 {net_sum:+.4f}；"
            f"目前 INITIAL_CAPITAL={config.INITIAL_CAPITAL_USDT}。"
            f"早期餘額貼近 100 而後跳至 ~500，符合本金調升，**無獨立資金調整流水**"
        )
        reason_sum_ok = n_close == sum(exit_dist.values())
    except Exception as exc:
        n_close = 0
        reason_sum_ok = False
        fee_note = f"讀取失敗：{exc}"
        capital_note = fee_note
        opens = pd.DataFrame()
        closed = pd.DataFrame()

    lines.append("| 檢查項 | 結果 | 檔案／函式 | 證據／說明 | 類型 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        f"| entry/exit 費用各扣一次 | PASS（模型） | `_calc_pnl` / `close_position` | "
        f"{fee_note}。無分離 entry_fee/exit_fee 欄位；等同一次記 round-trip。"
        f"浮動權益也預扣 round-trip，但平倉後浮動消失→**不雙記入已實現** | — |"
    )
    lines.append(
        "| balance_after_u 語意 | PASS（需釐清） | `calc_equity` + close log | "
        "寫入值為 `INITIAL + realized_net + floating`（權益）。"
        "開倉當下 floating 含預估費故略低於現金感。"
        "不是純現金帳戶 | 缺少紀錄（未標欄位語意於舊CSV） |"
    )
    lines.append(
        f"| 100→500 本金調整 | UNKNOWN | config / live_trades_log | "
        f"{capital_note}。不可把跳升當獲利；標記 **未對帳差額／資金調整未入帳** | 缺少紀錄 |"
    )
    lines.append(
        f"| 出場原因加總=平倉數 | {'PASS' if reason_sum_ok else 'FAIL'} | live_trades_log | "
        f"分布={exit_dist}；closed={n_close} | — |"
    )
    # SL vs losing
    try:
        closed2 = closed.copy()
        closed2["net"] = pd.to_numeric(closed2["net_pnl_u"], errors="coerce")
        closed2["gross"] = pd.to_numeric(closed2["gross_pnl_u"], errors="coerce")
        sl = closed2[closed2["exit_reason"].astype(str).str.contains("SL", na=False)]
        sl_win = int((sl["net"] > 0).sum())
        sl_lose = int((sl["net"] < 0).sum())
        lose_all = int((closed2["net"] < 0).sum())
        lines.append(
            f"| SL類 vs 虧損筆數分離 | PASS（統計應分離） | review stats | "
            f"含SL字樣出場 {len(sl)}（淨利>0: {sl_win}，淨損: {sl_lose}）；"
            f"全部淨損筆數 {lose_all}。保利停損可為正收益，不可等同虧損單 | — |"
        )
    except Exception:
        lines.append("| SL類 vs 虧損筆數分離 | UNKNOWN | — | 無法計算 | 缺少紀錄 |")

    lines.append("")
    lines.append("### OPEN/CLOSE 配對抽樣")
    try:
        lines.append(f"- OPEN={len(opens)} CLOSE={len(closed)}（差額可能為未平倉或中斷）")
    except Exception:
        lines.append("- UNKNOWN")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 總結：影響成交可信度的 FAIL／風險")
    lines.append("")
    lines.append("1. **同根K先更新MFE/保本再觸發有效SL**（`AMBIGUOUS_INTRABAR_ARM_AND_STOP`）— 模擬限制／潛在缺陷。")
    lines.append("   - 最小修補方案（待確認）：同根內若 lock 剛因該根 high/low 才啟動，則該根禁止用另一端觸發新有效SL；或標記歧義並提供樂觀/保守邊界，**不改歷史成交價**。")
    lines.append("2. **BREAKEVEN_WIN 依賴 gross>0** — 標籤語意缺陷；最小修補：以 trigger type 為準。")
    lines.append("3. **本金 100→500 無調整流水** — 缺紀錄；勿回填成獲利。")
    lines.append("")
    lines.append("以上 FAIL **尚未改成交規則、未改歷史**。待使用者確認後再修。")
    lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AI review bundle
# ---------------------------------------------------------------------------

def write_ai_review_bundle() -> Path:
    path = _out("ai_review_bundle.md")
    lines: list[str] = []
    lines.append("# AI 復盤精簡報告（ai_review_bundle）")
    lines.append("")
    lines.append(f"> 產生時間：{_now_iso()}")
    lines.append(f"> run_id：`{get_run_id()}`｜strategy_version：`{get_strategy_version()}`｜config_hash：`{config_hash()}`")
    lines.append("")
    lines.append("> 不同時間／訊號環境的版本績效**不是**嚴格 A/B 測試。")
    lines.append("")

    # A
    lines.append("## A. 資料品質")
    lines.append("")
    lines.append(f"- 詳見 `output/execution_audit.md`")
    lines.append(f"- 成交模擬：1m OHLC；同根 TP+SL → SL 優先；TRAIL 用 close")
    lines.append(f"- 成本：round-trip fee={config.ROUND_TRIP_FEE_RATE}（平倉一次計入）")
    lines.append(f"- 滑價模型：NONE")
    lines.append("")

    # B performance
    lines.append("## B. 績效（依門派／方向）")
    lines.append("")
    try:
        df = pd.read_csv(_out("live_trades_log.csv"))
        closed = df[df["event"].astype(str).str.upper() == "CLOSE"].copy()
        for col in ("net_pnl_u", "gross_pnl_u", "fee_u", "hold_mins", "roi_pct"):
            closed[col] = pd.to_numeric(closed[col], errors="coerce")
        if closed.empty:
            lines.append("_無平倉資料_")
        else:
            # version: legacy
            closed["strategy_version"] = "LEGACY_UNKNOWN"
            lines.append(f"- 平倉筆數：{len(closed)}（版本標記：歷史皆 LEGACY_UNKNOWN）")
            wins = closed[closed["net_pnl_u"] > 0]
            losses = closed[closed["net_pnl_u"] < 0]
            gross = float(closed["gross_pnl_u"].sum())
            fees = float(closed["fee_u"].sum())
            net = float(closed["net_pnl_u"].sum())
            avg_win = float(wins["net_pnl_u"].mean()) if len(wins) else 0.0
            avg_loss = float(losses["net_pnl_u"].mean()) if len(losses) else 0.0
            pf = (float(wins["net_pnl_u"].sum()) / abs(float(losses["net_pnl_u"].sum()))) if len(losses) and losses["net_pnl_u"].sum() != 0 else float("inf")
            # equity curve on realized only (approx using cumsum nets) — note capital jump
            curve = closed.sort_values("timestamp")["net_pnl_u"].cumsum()
            # max drawdown on cumulative net (not capital-adjusted)
            peak = curve.cummax()
            dd = curve - peak
            max_dd = float(dd.min()) if len(dd) else 0.0
            lines.append(f"- 勝/負：{len(wins)}/{len(losses)}｜勝率 {len(wins)/len(closed)*100:.1f}%")
            lines.append(f"- 毛利 {gross:+.4f}｜費用 {fees:.4f}｜淨損益 {net:+.4f}｜每筆期望 {net/len(closed):+.4f}")
            lines.append(f"- 平均獲利 {avg_win:+.4f}｜平均虧損 {avg_loss:+.4f}｜PF {pf if pf!=float('inf') else 'inf'}")
            lines.append(f"- 已實現累積淨利曲線最大回撤（僅 cum net，**未還原100→500本金**）：{max_dd:+.4f} U")
            lines.append(f"- 含浮盈回撤：UNKNOWN（歷史無每分鐘權益快照檔）")
            lines.append(f"- 平均持倉：{float(closed['hold_mins'].mean()):.2f} m")
            reasons = closed["exit_reason"].fillna("-").astype(str).value_counts().to_dict()
            lines.append(f"- 出場原因：{reasons}｜加總={sum(reasons.values())} vs 平倉={len(closed)}")
            lines.append(f"- 幣種數：{closed['symbol'].nunique()}")
            if len(wins):
                best = float(wins["net_pnl_u"].max())
                lines.append(f"- 最大單筆獲利 {best:+.4f}｜占總獲利比 {best/float(wins['net_pnl_u'].sum())*100 if wins['net_pnl_u'].sum() else 0:.1f}%")
                excl = net - best
                lines.append(f"- 排除最佳一筆後淨損益：{excl:+.4f}")
            lines.append("")
            lines.append("### 分組：門派 × 方向")
            lines.append("")
            lines.append("| school | side | n | win | net |")
            lines.append("|---|---|---:|---:|---:|")
            for (sch, side), g in closed.groupby(["school", "side"]):
                w = int((g["net_pnl_u"] > 0).sum())
                lines.append(f"| {sch} | {side} | {len(g)} | {w} | {float(g['net_pnl_u'].sum()):+.4f} |")
    except Exception as exc:
        lines.append(f"_績效計算失敗：{exc}_")

    lines.append("")
    lines.append("## C. 逐筆診斷（摘要）")
    lines.append("")
    lines.append("- 完整逐筆請見 `output/live_trades_log.csv` 與新 `trade_audit_*.jsonl`（新單才有完整進場快照）。")
    lines.append("- 歷史單缺少訊號K open_time／量倍分母／分流快照 → 欄位為 UNKNOWN，禁止回填。")
    lines.append("- 不可僅憑 MFE/MAE 宣稱換參數一定能賺。")
    lines.append("")

    lines.append("## D. 錯失機會與分流（含 MMTUSDT）")
    lines.append("")
    try:
        summary = Path(config.SUMMARY_CSV)
        plans = Path(config.ROUTING_PLAN_JSON)
        focus = ["MMTUSDT"]
        if summary.exists():
            sdf = pd.read_csv(summary)
            if "price_change_pct_24h" in sdf.columns:
                sdf["abs_chg"] = pd.to_numeric(sdf["price_change_pct_24h"], errors="coerce").abs()
                top = sdf.sort_values("abs_chg", ascending=False).head(8)
                for _, r in top.iterrows():
                    focus.append(str(r["symbol"]))
        focus = list(dict.fromkeys(focus))
        plan_map = {}
        if plans.exists():
            raw = json.loads(plans.read_text(encoding="utf-8"))
            # may be list or dict
            items = raw if isinstance(raw, list) else raw.get("plans") or raw.get("coins") or []
            if isinstance(items, list):
                for p in items:
                    if isinstance(p, dict) and p.get("symbol"):
                        plan_map[str(p["symbol"])] = p
        closed_syms = set()
        try:
            cdf = pd.read_csv(_out("live_trades_log.csv"))
            closed_syms = set(cdf["symbol"].astype(str))
        except Exception:
            pass
        lines.append("| symbol | 24h% | primary | status | 曾交易 | 決策可追溯 |")
        lines.append("|---|---:|---|---|---|---|")
        for sym in focus:
            p = plan_map.get(sym, {})
            chg = p.get("price_change_pct_24h", "UNKNOWN")
            if chg == "UNKNOWN" and summary.exists():
                try:
                    row = pd.read_csv(summary)
                    hit = row[row["symbol"] == sym]
                    if len(hit):
                        chg = float(hit.iloc[0]["price_change_pct_24h"])
                except Exception:
                    pass
            lines.append(
                f"| {sym} | {chg} | {p.get('primary_school', 'UNKNOWN')} | "
                f"{p.get('status', 'UNKNOWN')} | {'Y' if sym in closed_syms else 'N'} | "
                f"{'僅新決策檔' if False else '歷史缺 decision_trace → 無法完整追溯'} |"
            )
        lines.append("")
        lines.append("- 行情漲幅 ≠ 當時可交易收益；未使用未來資料選入場點。")
        lines.append("- 決策追蹤自本版起寫入 `decision_trace_YYYYMMDD.jsonl`。")
    except Exception as exc:
        lines.append(f"_分流摘要失敗：{exc}_")

    lines.append("")
    lines.append("## E. 附件索引")
    lines.append("")
    for p in [
        "output/execution_audit.md",
        "output/run_manifest.json",
        "output/live_trades_log.csv",
        "output/daily_review_for_ai.md",
        "output/kline_slices_for_ai.md",
        "output/active_routing_plan.json",
        f"output/decision_trace_{_today_tag()}.jsonl",
        f"output/trade_audit_{_today_tag()}.jsonl",
        "output/kline_archives/",
    ]:
        exists = Path(p).exists() if not p.endswith("/") else Path(p).exists()
        lines.append(f"- `{'[OK]' if exists else '[PENDING]'}` `{p}`")
    lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def bootstrap_audit_outputs() -> dict[str, str]:
    """啟動或手動呼叫：對齊 strategy_version 的 manifest／audit／bundle。"""
    man = ensure_run_manifest_for_current_strategy()
    a = write_execution_audit_md()
    b = write_ai_review_bundle()
    return {
        "run_id": man["run_id"],
        "strategy_version": man.get("strategy_version"),
        "previous_run_id": man.get("previous_run_id"),
        "execution_audit": str(a),
        "ai_review_bundle": str(b),
        "run_manifest": "output/run_manifest.json",
    }
