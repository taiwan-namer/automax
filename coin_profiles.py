"""
加密貨幣股性資料庫（防膨脹）

- coin_profiles_vault.csv / .md：以 symbol 為主鍵 Upsert（1 幣 1 行）
- coin_profiles_history.csv：僅在「有意義變化」時 Append
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import config

VAULT_COLUMNS: list[str] = [
    "symbol",
    "seen_count",
    "first_seen",
    "last_seen",
    "board",
    "status",
    "primary_school",
    "price_change_pct_24h",
    "quote_volume_24h_wan",
    "price_position_pct",
    "atr_1m_pct",
    "atr_15m_pct",
    "pulse_index",
    "avg_upper_wick_15m",
    "avg_lower_wick_15m",
    "long_wick_freq",
    "spike_count",
    "cont_rate_5m",
    "rev_rate_5m",
    "rev_rate_15m",
    "streak3_cont_rate",
    "streak4_rev_rate",
    "streak4_avg_rev_pct",
    "recent_volume_ratio",
    "skip_reason",
]


def _now_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _f(v: object, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


_STR_COLS = {
    "symbol",
    "board",
    "status",
    "primary_school",
    "first_seen",
    "last_seen",
    "skip_reason",
}


def _load_vault() -> pd.DataFrame:
    path = Path(config.COIN_PROFILES_VAULT_CSV)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame({c: pd.Series(dtype="object" if c in _STR_COLS else "float64") for c in VAULT_COLUMNS})
    df = pd.read_csv(path)
    for c in VAULT_COLUMNS:
        if c not in df.columns:
            df[c] = "" if c in _STR_COLS else 0.0
    for c in _STR_COLS:
        if c in df.columns:
            df[c] = df[c].fillna("").astype(str)
    return df


def _should_append_history(prev: pd.Series | None, new_row: dict[str, Any]) -> tuple[bool, str]:
    """
    有變化才新增歷史：
      (1) 首次入庫
      (2) 分派門派／狀態改變
      (3) 24h 漲跌幅變動超過 5%
      (4) 距離上次寫入已超過 4 小時
    """
    if prev is None:
        return True, "首次入庫"
    prev_school = str(prev.get("primary_school", "") or "")
    new_school = str(new_row.get("primary_school", "") or "")
    prev_status = str(prev.get("status", "") or "")
    new_status = str(new_row.get("status", "") or "")
    if prev_school != new_school or prev_status != new_status:
        return True, f"門派/狀態變更 {prev_status}/{prev_school} → {new_status}/{new_school}"

    prev_chg = _f(prev.get("price_change_pct_24h"))
    new_chg = _f(new_row.get("price_change_pct_24h"))
    if abs(new_chg - prev_chg) >= config.PROFILE_HISTORY_CHANGE_PCT:
        return True, f"24h漲跌幅變動 {prev_chg:.2f}% → {new_chg:.2f}%"

    # 時間間隔：以 last_seen 為準
    last_seen = str(prev.get("last_seen", "") or "")
    if last_seen:
        try:
            last_dt = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
            # 本地時間字串無 tz；用差值絕對值即可
            now_dt = datetime.now().replace(tzinfo=None)
            hours = abs((now_dt - last_dt).total_seconds()) / 3600.0
            if hours >= config.PROFILE_HISTORY_MIN_HOURS:
                return True, f"距上次寫入已 {hours:.1f} 小時"
        except ValueError:
            return True, "上次時間無法解析，補記一筆"

    return False, "無顯著變化（僅更新主表）"


def _append_history(row: dict[str, Any], reason: str) -> None:
    path = Path(config.COIN_PROFILES_HISTORY_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot_time": _now_str(),
        "append_reason": reason,
        **{c: row.get(c, "") for c in VAULT_COLUMNS},
    }
    df = pd.DataFrame([payload])
    write_header = not path.exists() or path.stat().st_size == 0
    df.to_csv(path, mode="a", header=write_header, index=False)


def _write_vault_md(df: pd.DataFrame) -> Path:
    """1 幣 1 行精煉 Markdown，供直接貼給 AI。"""
    path = Path(config.COIN_PROFILES_VAULT_MD)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = _now_str()
    ordered = df.sort_values(["seen_count", "last_seen"], ascending=[False, False]).reset_index(drop=True)

    lines = [
        "# 加密貨幣股性資料庫（Coin Profiles Vault）",
        "",
        f"> 更新時間：{now}",
        f"> 設計：**1 支幣 1 行 Upsert**（不會因每 10 分鐘重刷而重複膨脹）",
        f"> 目前不重複幣種數：{len(ordered)}",
        f"> 歷史快照：`{config.COIN_PROFILES_HISTORY_CSV}`（有變化才新增）",
        f"> K 線金庫：`{config.KLINES_VAULT_DIR}/`（1m=24h／15m=7天，增量去重覆寫）",
        "",
        "## 股性總表（精煉）",
        "",
        "| 幣種 | 累積上榜次數 | 首次/最近上榜 | 榜單 | 狀態/主門派 | 24h漲跌% | 成交額(萬U) | 價格位置% | 脈衝 | 量能比 | 插針長針% | 5m延續/反轉 | 15m騙炮率 | 連4反轉率 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for _, r in ordered.iterrows():
        first = str(r.get("first_seen", "-"))
        last = str(r.get("last_seen", "-"))
        span = f"{first} / {last}"
        status = str(r.get("status", ""))
        school = str(r.get("primary_school", ""))
        status_s = school if status == "ROUTED" else status
        lw = _f(r.get("long_wick_freq")) * 100.0
        lines.append(
            "| {sym} | {seen} | {span} | {board} | {st} | {chg:.2f}% | {vol:.1f} | {pos:.1f}% | "
            "{pulse:.3f} | {vr:.3f} | {lw:.1f}% | {c5:.1f}%/{r5:.1f}% | {r15:.1f}% | {s4:.1f}% |".format(
                sym=r.get("symbol"),
                seen=int(_f(r.get("seen_count"), 1)),
                span=span,
                board=r.get("board", ""),
                st=status_s,
                chg=_f(r.get("price_change_pct_24h")),
                vol=_f(r.get("quote_volume_24h_wan")),
                pos=_f(r.get("price_position_pct")),
                pulse=_f(r.get("pulse_index")),
                vr=_f(r.get("recent_volume_ratio")),
                lw=lw,
                c5=_f(r.get("cont_rate_5m")),
                r5=_f(r.get("rev_rate_5m")),
                r15=_f(r.get("rev_rate_15m")),
                s4=_f(r.get("streak4_rev_rate")),
            )
        )
    lines.extend(
        [
            "",
            "## 給 AI 的門派研發 Prompt",
            "",
            "```",
            "請根據上方「不重複妖幣」股性總表（含累積上榜次數與最新門派／股性指標）：",
            "1. 歸納高頻上榜幣的共同莊家行為模式；",
            "2. 建議是否需要新增第 5／第 6 個門派，並給出入選條件與 TP/SL 草案；",
            "3. 指出哪些幣應永久加入黑名單（量縮／騙炮過高／過熱）。",
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def upsert_coin_profiles(
    plans: list[dict[str, Any]],
    summary_csv: str | Path | None = None,
) -> tuple[Path, Path, dict[str, int]]:
    """
    依本輪 Top20 分派結果 Upsert 主表，並智慧寫入歷史。
    回傳 (vault_md, history_csv, stats)。
    """
    summary_csv = Path(summary_csv or config.SUMMARY_CSV)
    summary = pd.read_csv(summary_csv) if summary_csv.exists() else pd.DataFrame()
    summary_map = {str(r["symbol"]): r for _, r in summary.iterrows()} if not summary.empty else {}

    vault = _load_vault()
    by_sym = {str(r["symbol"]): i for i, r in vault.iterrows()} if not vault.empty else {}

    now = _now_str()
    hist_appended = 0
    updated = 0
    created = 0

    for plan in plans:
        sym = str(plan.get("symbol", ""))
        if not sym:
            continue
        src = summary_map.get(sym, {})
        primary = str(plan.get("primary_school") or plan.get("status") or "")
        status = str(plan.get("status") or "")
        new_row: dict[str, Any] = {
            "symbol": sym,
            "board": plan.get("board") or src.get("board", ""),
            "status": status,
            "primary_school": primary if status == "ROUTED" else status,
            "price_change_pct_24h": _f(plan.get("price_change_pct_24h", src.get("price_change_pct_24h"))),
            "quote_volume_24h_wan": _f(plan.get("quote_volume_24h_wan", src.get("quote_volume_24h_wan"))),
            "price_position_pct": _f(plan.get("price_position_pct", src.get("price_position_pct"))),
            "atr_1m_pct": _f(src.get("atr_1m_pct")),
            "atr_15m_pct": _f(src.get("atr_15m_pct")),
            "pulse_index": _f(src.get("pulse_index")),
            "avg_upper_wick_15m": _f(src.get("avg_upper_wick_15m")),
            "avg_lower_wick_15m": _f(src.get("avg_lower_wick_15m")),
            "long_wick_freq": _f(src.get("long_wick_freq")),
            "spike_count": int(_f(src.get("spike_count"))),
            "cont_rate_5m": _f(src.get("cont_rate_5m")),
            "rev_rate_5m": _f(src.get("rev_rate_5m")),
            "rev_rate_15m": _f(src.get("rev_rate_15m")),
            "streak3_cont_rate": _f(src.get("streak3_cont_rate")),
            "streak4_rev_rate": _f(src.get("streak4_rev_rate")),
            "streak4_avg_rev_pct": _f(src.get("streak4_avg_rev_pct")),
            "recent_volume_ratio": _f(plan.get("recent_volume_ratio", src.get("recent_volume_ratio"))),
            "skip_reason": plan.get("skip_reason", ""),
            "last_seen": now,
        }

        if sym in by_sym:
            idx = by_sym[sym]
            prev = vault.loc[idx]
            seen = int(_f(prev.get("seen_count"), 0)) + 1
            first_seen = str(prev.get("first_seen") or now)
            new_row["seen_count"] = seen
            new_row["first_seen"] = first_seen
            should, reason = _should_append_history(prev, new_row)
            for c in VAULT_COLUMNS:
                val = new_row.get(c, "")
                if c in _STR_COLS:
                    vault.at[idx, c] = str(val if val is not None else "")
                else:
                    vault.at[idx, c] = _f(val)
            updated += 1
        else:
            new_row["seen_count"] = 1
            new_row["first_seen"] = now
            add = pd.DataFrame([{c: new_row.get(c, "" if c in _STR_COLS else 0) for c in VAULT_COLUMNS}])
            if vault.empty:
                vault = add.copy()
            else:
                vault = pd.concat([vault, add], ignore_index=True)
            by_sym[sym] = len(vault) - 1
            should, reason = True, "首次入庫"
            created += 1

        if should:
            _append_history(new_row, reason)
            hist_appended += 1

    # 正規化欄位順序並覆寫主表
    for c in VAULT_COLUMNS:
        if c not in vault.columns:
            vault[c] = ""
    vault = vault[VAULT_COLUMNS]
    vault_csv = Path(config.COIN_PROFILES_VAULT_CSV)
    vault_csv.parent.mkdir(parents=True, exist_ok=True)
    vault.to_csv(vault_csv, index=False)

    md_path = _write_vault_md(vault)
    stats = {
        "symbols": len(vault),
        "created": created,
        "updated": updated,
        "history_appended": hist_appended,
    }
    return md_path, Path(config.COIN_PROFILES_HISTORY_CSV), stats
