"""
自動策略分派器（Strategy Router）

讀取最新 Top50 幣股性數據，依過濾規則分派門派與動態 TP/SL 參數，
產出 output/active_routing_plan.md。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import config

# 門派代碼
SCHOOL_SKIP = "SKIP"
SCHOOL_WATCH = "WATCH_ONLY"
SCHOOL_MEAN_REV = "MEAN_REV_15M"
SCHOOL_SPIKE_FADE = "SPIKE_FADE_1M"
SCHOOL_SPIKE_MOMO = "SPIKE_MOMO_1M"
SCHOOL_TREND = "TREND_FOLLOW_15M"
SCHOOL_SWING_REV = "SWING_REV_15M"

# 交易門派優先序（同幣多門派時，盯盤以優先序最高者為主；其餘列為備援）
SCHOOL_PRIORITY: list[str] = [
    SCHOOL_MEAN_REV,
    SCHOOL_SPIKE_FADE,
    SCHOOL_SPIKE_MOMO,
    SCHOOL_TREND,
    SCHOOL_SWING_REV,
]

SCHOOL_LABELS: dict[str, str] = {
    SCHOOL_MEAN_REV: "門派1｜15m 連四反轉派",
    SCHOOL_SPIKE_FADE: "門派2｜1m 爆量騙炮反殺派",
    SCHOOL_SPIKE_MOMO: "門派3｜1m 爆量動能直追派",
    SCHOOL_TREND: "門派4｜15m 順勢追擊派",
    SCHOOL_SWING_REV: "門派5｜15m 延遲畫門波段派（右側結構轉折）",
}


def _f(row: pd.Series | dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        if isinstance(row, dict):
            v = row.get(key, default)
        else:
            v = row[key] if key in row.index else default
        return float(v)
    except (TypeError, ValueError, KeyError):
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _params_mean_rev(row: pd.Series | dict[str, Any]) -> dict[str, float]:
    return {
        "tp_pct": float(config.MEAN_REV_TP_PCT),
        "sl_pct": float(config.MEAN_REV_SL_PCT),
        "max_hold_mins": int(config.MEAN_REV_MAX_HOLD_MINS),
    }


def _params_spike_fade(row: pd.Series | dict[str, Any]) -> dict[str, float]:
    rev_move = _f(row, "rev_avg_pct_15m")
    atr1 = _f(row, "atr_1m_pct")
    # 無有效波動特徵時回落預設 0.95 / 0.70
    tp_raw = rev_move * 0.8 if rev_move > 0 else float(config.SPIKE_FADE_TP_DEFAULT)
    sl_raw = atr1 * 2.2 if atr1 > 0 else float(config.SPIKE_FADE_SL_DEFAULT)
    return {
        "tp_pct": round(
            _clamp(tp_raw, config.SPIKE_FADE_TP_MIN, config.SPIKE_FADE_TP_MAX), 4
        ),
        "sl_pct": round(
            _clamp(sl_raw, config.SPIKE_FADE_SL_MIN, config.SPIKE_FADE_SL_MAX), 4
        ),
        "max_hold_mins": int(config.SPIKE_FADE_MAX_HOLD_MINS),
    }


def _params_spike_momo(row: pd.Series | dict[str, Any]) -> dict[str, float]:
    return {
        "tp_pct": float(config.SPIKE_MOMO_TP_PCT),
        "sl_pct": float(config.SPIKE_MOMO_SL_PCT),
        "max_hold_mins": int(config.SPIKE_MOMO_MAX_HOLD_MINS),
    }


def _params_trend(row: pd.Series | dict[str, Any]) -> dict[str, float]:
    return {
        "tp_pct": float(config.TREND_TP_PCT),
        "sl_pct": float(config.TREND_SL_PCT),
        "max_hold_mins": int(config.TREND_MAX_HOLD_MINS),
    }


def _params_swing_rev(row: pd.Series | dict[str, Any]) -> dict[str, float]:
    """門派5：固定波段 TP/SL（右側結構轉折）。"""
    return {
        "tp_pct": float(config.SWING_TP_PCT),
        "sl_pct": float(config.SWING_SL_PCT),
        "max_hold_mins": int(config.SWING_MAX_HOLD_MINS),
    }


def _match_schools(row: pd.Series | dict[str, Any]) -> list[dict[str, Any]]:
    """回傳該幣符合的所有交易門派（含參數與入選理由）。"""
    matched: list[dict[str, Any]] = []
    chg_24h = _f(row, "price_change_pct_24h")
    anti_sq = float(getattr(config, "ANTI_SQUEEZE_CHG_PCT", 50.0))
    is_extreme_gainer = chg_24h > anti_sq

    streak4_rev = _f(row, "streak4_rev_rate")
    streak4_move = _f(row, "streak4_avg_rev_pct")
    if streak4_rev >= config.MEAN_REV_STREAK4_REV_RATE_GE and streak4_move >= config.MEAN_REV_STREAK4_REV_MOVE_GE:
        matched.append(
            {
                "school": SCHOOL_MEAN_REV,
                "reason": (
                    f"streak4_rev_rate={streak4_rev:.1f}%≥{config.MEAN_REV_STREAK4_REV_RATE_GE}% "
                    f"且 streak4_avg_rev_pct={streak4_move:.2f}%≥{config.MEAN_REV_STREAK4_REV_MOVE_GE}%"
                ),
                "params": _params_mean_rev(row),
            }
        )

    rev15 = _f(row, "rev_rate_15m")
    pulse = _f(row, "pulse_index")
    rev5 = _f(row, "rev_rate_5m")
    # 防軋空：24h 漲幅 > 50% 禁止 SPIKE_FADE 摸頂做空（分派層）
    if (not is_extreme_gainer) and rev15 >= config.SPIKE_FADE_REV_RATE_15M_GE and (
        pulse >= config.SPIKE_FADE_PULSE_GE or rev5 >= config.SPIKE_FADE_REV_RATE_5M_GE
    ):
        matched.append(
            {
                "school": SCHOOL_SPIKE_FADE,
                "reason": (
                    f"rev_rate_15m={rev15:.1f}%≥{config.SPIKE_FADE_REV_RATE_15M_GE}% "
                    f"且 (pulse={pulse:.3f}≥{config.SPIKE_FADE_PULSE_GE} 或 "
                    f"rev_rate_5m={rev5:.1f}%≥{config.SPIKE_FADE_REV_RATE_5M_GE}%)"
                ),
                "params": _params_spike_fade(row),
            }
        )

    cont5 = _f(row, "cont_rate_5m")
    pos = _f(row, "price_position_pct")
    vol_ratio = _f(row, "recent_volume_ratio")
    # MOMO 常規：15m騙炮率低 + 5m延續高 + 量能比合格
    if (
        rev15 <= config.SPIKE_MOMO_REV_RATE_15M_LE
        and cont5 >= config.SPIKE_MOMO_CONT_RATE_5M_GE
        and vol_ratio >= config.SPIKE_MOMO_VOLUME_RATIO_GE
        and pos < config.SPIKE_MOMO_PRICE_POS_LT
    ):
        matched.append(
            {
                "school": SCHOOL_SPIKE_MOMO,
                "reason": (
                    f"rev_rate_15m={rev15:.1f}%≤{config.SPIKE_MOMO_REV_RATE_15M_LE}% "
                    f"且 cont_rate_5m={cont5:.1f}%≥{config.SPIKE_MOMO_CONT_RATE_5M_GE}% "
                    f"且 volume_ratio={vol_ratio:.3f}≥{config.SPIKE_MOMO_VOLUME_RATIO_GE}"
                ),
                "params": _params_spike_momo(row),
            }
        )

    streak3_cont = _f(row, "streak3_cont_rate")
    if streak3_cont >= config.TREND_STREAK3_CONT_RATE_GE and vol_ratio >= config.TREND_VOLUME_RATIO_GE:
        matched.append(
            {
                "school": SCHOOL_TREND,
                "reason": (
                    f"streak3_cont_rate={streak3_cont:.1f}%≥{config.TREND_STREAK3_CONT_RATE_GE}% "
                    f"且 recent_volume_ratio={vol_ratio:.3f}≥{config.TREND_VOLUME_RATIO_GE}"
                ),
                "params": _params_trend(row),
            }
        )

    # 門派 5 SWING：暫時封印
    if bool(getattr(config, "SWING_REV_ENABLED", False)):
        atr15 = _f(row, "atr_15m_pct")
        swing_ok = (
            rev15 >= config.SWING_REV_RATE_15M_GE
            and vol_ratio >= config.SWING_VOLUME_RATIO_GE
            and atr15 >= config.SWING_ATR_15M_GE
            and pulse >= config.SWING_PULSE_GE
        )
        if swing_ok:
            matched.append(
                {
                    "school": SCHOOL_SWING_REV,
                    "reason": (
                        f"rev_rate_15m={rev15:.1f}%≥{config.SWING_REV_RATE_15M_GE}% "
                        f"且 volume_ratio={vol_ratio:.3f}≥{config.SWING_VOLUME_RATIO_GE}"
                    ),
                    "params": _params_swing_rev(row),
                }
            )

    # —— 強制解放強勢帶量突破幣：MOMO 主 + TREND 副 ——
    force_momo = (
        vol_ratio >= float(getattr(config, "FORCE_MOMO_VOLUME_RATIO_GE", 1.5))
        and (
            pos >= float(getattr(config, "FORCE_MOMO_POS_HIGH_GE", 70.0))
            or pos <= float(getattr(config, "FORCE_MOMO_POS_LOW_LE", 30.0))
        )
    )
    if force_momo:
        schools_have = {m["school"] for m in matched}
        force_reason = (
            f"強制順勢：volume_ratio={vol_ratio:.3f}≥{config.FORCE_MOMO_VOLUME_RATIO_GE} "
            f"且 price_position={pos:.1f}%（≥{config.FORCE_MOMO_POS_HIGH_GE}% 或 "
            f"≤{config.FORCE_MOMO_POS_LOW_LE}%）"
        )
        if SCHOOL_SPIKE_MOMO not in schools_have:
            matched.append(
                {
                    "school": SCHOOL_SPIKE_MOMO,
                    "reason": force_reason,
                    "params": _params_spike_momo(row),
                }
            )
        else:
            # 標註強制理由（保留原 params）
            for m in matched:
                if m["school"] == SCHOOL_SPIKE_MOMO:
                    m["reason"] = force_reason + "｜" + m["reason"]
                    break
        if SCHOOL_TREND not in schools_have:
            matched.append(
                {
                    "school": SCHOOL_TREND,
                    "reason": force_reason + "｜副門派 TREND",
                    "params": _params_trend(row),
                }
            )
        # 強勢突破幣剔除逆勢 FADE，避免摸頂
        matched = [m for m in matched if m["school"] != SCHOOL_SPIKE_FADE]
        # 主門派強制 MOMO，副門派 TREND 次之
        momo = [m for m in matched if m["school"] == SCHOOL_SPIKE_MOMO]
        trend = [m for m in matched if m["school"] == SCHOOL_TREND]
        others = [
            m
            for m in matched
            if m["school"] not in (SCHOOL_SPIKE_MOMO, SCHOOL_TREND, SCHOOL_SWING_REV)
        ]
        matched = momo + trend + others
        return matched

    # 一般排序；極端強勢且未觸發 force_momo 時仍剔除 FADE
    matched = [m for m in matched if m["school"] != SCHOOL_SWING_REV or config.SWING_REV_ENABLED]
    matched.sort(key=lambda m: SCHOOL_PRIORITY.index(m["school"]))
    if is_extreme_gainer:
        matched = [m for m in matched if m["school"] != SCHOOL_SPIKE_FADE]
        # 優先順勢門派
        prefer = [m for m in matched if m["school"] in (SCHOOL_SPIKE_MOMO, SCHOOL_TREND)]
        rest = [m for m in matched if m["school"] not in (SCHOOL_SPIKE_MOMO, SCHOOL_TREND)]
        matched = prefer + rest
    return matched


def route_coin(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    """
    對單一幣種執行過濾與分派。
    回傳結構：
      symbol, board, rank, status, skip_reason,
      primary_school, schools[{school, reason, params}], features摘要
    """
    if isinstance(row, dict):
        symbol = str(row.get("symbol", ""))
        board = str(row.get("board", ""))
        rank = int(row.get("rank", 0) or 0)
    else:
        symbol = str(row.get("symbol", ""))
        board = str(row.get("board", ""))
        rank = int(row.get("rank", 0) or 0)

    vol_ratio = _f(row, "recent_volume_ratio")
    quote_wan = _f(row, "quote_volume_24h_wan")

    base = {
        "symbol": symbol,
        "board": board,
        "rank": rank,
        "price_change_pct_24h": _f(row, "price_change_pct_24h"),
        "quote_volume_24h_wan": quote_wan,
        "recent_volume_ratio": vol_ratio,
        "price_position_pct": _f(row, "price_position_pct"),
        "atr_1m_pct": _f(row, "atr_1m_pct"),
        "atr_15m_pct": _f(row, "atr_15m_pct"),
        "pulse_index": _f(row, "pulse_index"),
    }

    skip_reasons: list[str] = []
    if vol_ratio < config.SKIP_VOLUME_RATIO_LT:
        skip_reasons.append(
            f"recent_volume_ratio={vol_ratio:.3f}<{config.SKIP_VOLUME_RATIO_LT}（量縮退潮）"
        )
    if quote_wan < config.SKIP_QUOTE_VOLUME_WAN_LT:
        skip_reasons.append(
            f"24h成交額={quote_wan:.1f}萬U<{config.SKIP_QUOTE_VOLUME_WAN_LT}萬U"
        )

    if skip_reasons:
        return {
            **base,
            "status": SCHOOL_SKIP,
            "skip_reason": "；".join(skip_reasons),
            "primary_school": SCHOOL_SKIP,
            "schools": [],
            "tradeable": False,
        }

    matched = _match_schools(row)
    if not matched:
        return {
            **base,
            "status": SCHOOL_WATCH,
            "skip_reason": "",
            "primary_school": SCHOOL_WATCH,
            "schools": [],
            "tradeable": False,
        }

    return {
        **base,
        "status": "ROUTED",
        "skip_reason": "",
        "primary_school": matched[0]["school"],
        "schools": matched,
        "tradeable": True,
    }


def route_dataframe(df: pd.DataFrame) -> list[dict[str, Any]]:
    """對整份股性 DataFrame 分派。"""
    plans: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        plans.append(route_coin(row))
    # 漲幅榜在前
    board_order = {"gainer": 0, "loser": 1}
    plans.sort(key=lambda p: (board_order.get(p.get("board", ""), 9), p.get("rank", 99)))
    return plans


def write_routing_plan(
    plans: list[dict[str, Any]],
    md_path: str | Path | None = None,
) -> Path:
    """將分派結果寫成 Markdown。"""
    md_path = Path(md_path or config.ROUTING_PLAN_MD)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    skips = [p for p in plans if p["status"] == SCHOOL_SKIP]
    watches = [p for p in plans if p["status"] == SCHOOL_WATCH]
    routed = [p for p in plans if p["status"] == "ROUTED"]

    by_school: dict[str, list[dict[str, Any]]] = {s: [] for s in SCHOOL_PRIORITY}
    for p in routed:
        for m in p["schools"]:
            by_school[m["school"]].append({**p, "_match": m})

    lines: list[str] = []
    lines.append("# 自動策略分派計畫（Active Routing Plan）")
    lines.append("")
    lines.append(f"> 產生時間：{now}")
    lines.append(f"> 來源股性：`{config.SUMMARY_CSV}`")
    lines.append(f"> 總幣數：{len(plans)}｜可交易：{len(routed)}｜觀察：{len(watches)}｜剔除：{len(skips)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 垃圾桶名單（SKIP｜不盯盤）")
    lines.append("")
    if not skips:
        lines.append("_（無）_")
        lines.append("")
    else:
        lines.append("| 幣種 | 榜單 | 24h漲跌% | 成交額(萬U) | 量能比 | 剔除原因 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for p in skips:
            lines.append(
                f"| {p['symbol']} | {p['board']} | {p['price_change_pct_24h']:.2f}% | "
                f"{p['quote_volume_24h_wan']:.1f} | {p['recent_volume_ratio']:.3f} | {p['skip_reason']} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 觀察名單（WATCH_ONLY｜不下單）")
    lines.append("")
    if not watches:
        lines.append("_（無）_")
        lines.append("")
    else:
        lines.append("| 幣種 | 榜單 | 24h漲跌% | 價格位置% | 量能比 | 說明 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for p in watches:
            lines.append(
                f"| {p['symbol']} | {p['board']} | {p['price_change_pct_24h']:.2f}% | "
                f"{p['price_position_pct']:.1f}% | {p['recent_volume_ratio']:.3f} | 未符任何門派門檻 |"
            )
        lines.append("")

    for school in SCHOOL_PRIORITY:
        lines.append("---")
        lines.append("")
        lines.append(f"## {SCHOOL_LABELS[school]}（`{school}`）")
        lines.append("")
        items = by_school[school]
        if not items:
            lines.append("_（本輪無入選）_")
            lines.append("")
            continue
        lines.append("| 幣種 | 主/備援 | 榜單 | 入選條件 | TP% | SL% | 最長持倉(分) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for item in items:
            m = item["_match"]
            role = "主門派" if item["primary_school"] == school else "備援"
            params = m["params"]
            lines.append(
                f"| {item['symbol']} | {role} | {item['board']} | {m['reason']} | "
                f"{params['tp_pct']:.2f}% | {params['sl_pct']:.2f}% | {int(params['max_hold_mins'])} |"
            )
        lines.append("")
        lines.append("### 觸發條件摘要")
        lines.append("")
        if school == SCHOOL_MEAN_REV:
            lines.append("- 15m 連 4 紅 + 第 4 根上影線 ≥ 20% → **做空**")
            lines.append("- 15m 連 4 黑 + 第 4 根下影線 ≥ 20% → **做多**")
        elif school == SCHOOL_SPIKE_FADE:
            lines.append("- T-1 爆量紅 K +（上影線≥35% 或 T 收黑）→ 於 T 收盤 **做空**")
            lines.append("- T-1 爆量黑 K +（下影線≥35% 或 T 收紅）→ 於 T 收盤 **做多**")
        elif school == SCHOOL_SPIKE_MOMO:
            lines.append("- 1m 爆量且實體佔比 ≥ 60%：紅→**做多**，黑→**做空**")
        elif school == SCHOOL_TREND:
            lines.append("- 15m 連 3 紅且量 > 20 均量 → **做多**")
            lines.append("- 15m 連 3 黑且量 > 20 均量 → **做空**")
        else:
            lines.append("- **做空**：近 5 根創 20 根新高 + 爆量誘多 + 累積漲≥0.8%，當根收黑跌破前兩根低點")
            lines.append("- **做多**：近 5 根創 20 根新低 + 爆量急殺 + 累積跌≤-0.8%，當根收紅突破前兩根高點")
            lines.append("- 固定參數：TP 1.20%／SL 0.75%／最長持倉 25 分鐘")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 可交易監控池（以主門派為準）")
    lines.append("")
    lines.append("| 幣種 | 主門派 | TP% | SL% | 持倉上限(分) | 其他備援門派 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for p in routed:
        primary = next(m for m in p["schools"] if m["school"] == p["primary_school"])
        backups = [m["school"] for m in p["schools"] if m["school"] != p["primary_school"]]
        params = primary["params"]
        lines.append(
            f"| {p['symbol']} | {p['primary_school']} | {params['tp_pct']:.2f}% | "
            f"{params['sl_pct']:.2f}% | {int(params['max_hold_mins'])} | "
            f"{', '.join(backups) if backups else '-'} |"
        )
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def write_routing_plan_json(
    plans: list[dict[str, Any]],
    json_path: str | Path | None = None,
) -> Path:
    """同步寫出 active_routing_plan.json（供程式／外部讀取）。"""
    json_path = Path(json_path or config.ROUTING_PLAN_JSON)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    payload = {
        "generated_at": now,
        "refresh_interval_minutes": config.REFRESH_ROUTING_MINUTES,
        "tradeable": [p["symbol"] for p in plans if p.get("tradeable")],
        "watch_only": [p["symbol"] for p in plans if p.get("status") == SCHOOL_WATCH],
        "skipped": [
            {"symbol": p["symbol"], "reason": p.get("skip_reason", "")}
            for p in plans
            if p.get("status") == SCHOOL_SKIP
        ],
        "plans": plans,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return json_path


def run_router(
    csv_path: str | Path | None = None,
    md_path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], Path]:
    """讀取股性 CSV → 分派 → 寫入 routing plan（md + json）。"""
    csv_path = Path(csv_path or config.SUMMARY_CSV)
    if not csv_path.exists():
        raise FileNotFoundError(f"找不到股性摘要：{csv_path}，請先執行解剖流程。")
    df = pd.read_csv(csv_path)
    plans = route_dataframe(df)
    out = write_routing_plan(plans, md_path=md_path)
    write_routing_plan_json(plans)
    # 股性資料庫 Upsert（1 幣 1 行）+ 智慧歷史去重
    try:
        from coin_profiles import upsert_coin_profiles

        vault_md, hist_csv, stats = upsert_coin_profiles(plans, summary_csv=csv_path)
        print(
            f"📚 股性金庫已更新：不重複幣種 {stats['symbols']}｜"
            f"新增 {stats['created']}／更新 {stats['updated']}｜"
            f"歷史快照 +{stats['history_appended']} → {vault_md.name} / {hist_csv.name}"
        )
    except Exception as exc:
        print(f"⚠ 股性金庫更新失敗：{exc}")
    return plans, out
