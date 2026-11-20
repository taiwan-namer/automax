"""
AI 報告產生器

讀取 coin_personality_summary.csv，產出分為「漲幅榜 / 跌幅榜」的
純股性解剖報告，供使用者貼給 AI 構思策略。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config


def _fmt(val: object, kind: str = "num") -> str:
    try:
        if pd.isna(val):
            return "N/A"
        x = float(val)
    except (TypeError, ValueError):
        return str(val)

    if kind == "pct":
        return f"{x:.2f}%"
    if kind == "pct1":
        return f"{x:.1f}%"
    if kind == "ratio":
        return f"{x:.3f}"
    if kind == "wan":
        return f"{x:,.1f}"
    if kind == "int":
        return str(int(x))
    return f"{x:.2f}"


def _board_table(df: pd.DataFrame, title: str) -> list[str]:
    """將單一榜單轉成多段 Markdown 表格（避免單表過寬）。"""
    lines: list[str] = [f"## {title}", ""]
    if df.empty:
        lines.append("_（無資料）_")
        lines.append("")
        return lines

    lines.append("### 盤勢位置｜波動節奏｜量能熱度")
    lines.append("")
    lines.append(
        "| 排名 | 幣種 | 24h漲跌% | 成交額(萬U) | 價格位置% | 1m振幅% | 15m振幅% | 脈衝指數 | 近期量能比 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for _, r in df.iterrows():
        lines.append(
            "| {rank} | {sym} | {chg} | {vol} | {pos} | {a1} | {a15} | {pulse} | {vr} |".format(
                rank=int(r["rank"]),
                sym=r["symbol"],
                chg=_fmt(r["price_change_pct_24h"], "pct"),
                vol=_fmt(r["quote_volume_24h_wan"], "wan"),
                pos=_fmt(r["price_position_pct"], "pct1"),
                a1=_fmt(r["atr_1m_pct"], "pct"),
                a15=_fmt(r["atr_15m_pct"], "pct"),
                pulse=_fmt(r["pulse_index"], "ratio"),
                vr=_fmt(r["recent_volume_ratio"], "ratio"),
            )
        )
    lines.append("")

    lines.append("### 插針與洗盤習性（15m）")
    lines.append("")
    lines.append("| 排名 | 幣種 | 平均上影線比 | 平均下影線比 | 長針頻率 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for _, r in df.iterrows():
        lines.append(
            "| {rank} | {sym} | {up} | {lo} | {lw} |".format(
                rank=int(r["rank"]),
                sym=r["symbol"],
                up=_fmt(r["avg_upper_wick_15m"], "ratio"),
                lo=_fmt(r["avg_lower_wick_15m"], "ratio"),
                lw=_fmt(float(r["long_wick_freq"]) * 100.0, "pct"),
            )
        )
    lines.append("")

    lines.append("### 爆量後真實走向（1m 爆量 ×5 均量；不設停利停損）")
    lines.append("")
    lines.append(
        "| 排名 | 幣種 | 爆量次數 | "
        "3m延續率/均幅 | 3m反轉率/均幅 | "
        "5m延續率/均幅 | 5m反轉率/均幅 | "
        "15m延續率/均幅 | 15m反轉率/均幅 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for _, r in df.iterrows():

        def pair(rate_col: str, avg_col: str) -> str:
            return f"{_fmt(r[rate_col], 'pct')}/{_fmt(r[avg_col], 'pct')}"

        lines.append(
            "| {rank} | {sym} | {n} | {c3} | {r3} | {c5} | {r5} | {c15} | {r15} |".format(
                rank=int(r["rank"]),
                sym=r["symbol"],
                n=_fmt(r["spike_count"], "int"),
                c3=pair("cont_rate_3m", "cont_avg_pct_3m"),
                r3=pair("rev_rate_3m", "rev_avg_pct_3m"),
                c5=pair("cont_rate_5m", "cont_avg_pct_5m"),
                r5=pair("rev_rate_5m", "rev_avg_pct_5m"),
                c15=pair("cont_rate_15m", "cont_avg_pct_15m"),
                r15=pair("rev_rate_15m", "rev_avg_pct_15m"),
            )
        )
    lines.append("")

    lines.append("### 連續順勢極限（15m 連紅/連黑後）")
    lines.append("")
    lines.append(
        "| 排名 | 幣種 | 連3繼續率 | 連3反轉率 | 連3反轉均幅 | 連4繼續率 | 連4反轉率 | 連4反轉均幅 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for _, r in df.iterrows():
        lines.append(
            "| {rank} | {sym} | {c3} | {r3} | {a3} | {c4} | {r4} | {a4} |".format(
                rank=int(r["rank"]),
                sym=r["symbol"],
                c3=_fmt(r["streak3_cont_rate"], "pct"),
                r3=_fmt(r["streak3_rev_rate"], "pct"),
                a3=_fmt(r["streak3_avg_rev_pct"], "pct"),
                c4=_fmt(r["streak4_cont_rate"], "pct"),
                r4=_fmt(r["streak4_rev_rate"], "pct"),
                a4=_fmt(r["streak4_avg_rev_pct"], "pct"),
            )
        )
    lines.append("")
    return lines


def generate_report(
    csv_path: str | Path | None = None,
    md_path: str | Path | None = None,
) -> Path:
    csv_path = Path(csv_path or config.SUMMARY_CSV)
    md_path = Path(md_path or config.AI_REPORT_MD)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    if not csv_path.exists() or csv_path.stat().st_size == 0:
        md_path.write_text(
            "# 當紅小幣股性解剖報告\n\n尚無資料，請先執行 `python3 main.py`。\n",
            encoding="utf-8",
        )
        return md_path

    df = pd.read_csv(csv_path)
    if df.empty:
        md_path.write_text("# 當紅小幣股性解剖報告\n\nCSV 為空。\n", encoding="utf-8")
        return md_path

    gainers = df[df["board"] == "gainer"].sort_values("rank")
    losers = df[df["board"] == "loser"].sort_values("rank")

    lines: list[str] = []
    lines.append("# 幣安當紅小幣・純股性與莊家控盤行為解剖報告")
    lines.append("")
    lines.append(f"> 產生時間：{now}")
    lines.append(
        f"> 樣本：24h 漲幅 Top {config.TOP_GAINERS} + 跌幅 Top {config.TOP_LOSERS}（共 {len(df)} 支）"
    )
    lines.append(
        f"> K 線回看：1m={config.LOOKBACK_1M_HOURS}h／15m={config.LOOKBACK_15M_DAYS}天；"
        f"增量金庫 `{config.KLINES_VAULT_DIR}/`；股性主表 `{config.COIN_PROFILES_VAULT_MD}`"
    )
    lines.append("> 本報告**不含任何預設策略回測**，僅提供客觀股性數據供 AI 構思策略。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 指標速查")
    lines.append("")
    lines.append("- **價格位置%**：0%=24h 底部，100%=24h 頂部，50%=衝高回落一半")
    lines.append("- **脈衝指數**：過去 24h 最大 5 根 15m 實體變動佔總變動比（高=裝死突襲；低=沿趨勢走）")
    lines.append("- **爆量延續/反轉**：1m 量 > 前 20 均量×5 後，第 N 分鐘同向續走 vs 反向穿越開盤價")
    lines.append("- **近期量能比**：近 2h 均量 / 過去 24h 均量（>1.5 升溫；<0.4 量縮死魚）")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.extend(
        _board_table(gainers, f"【24h 漲幅榜 Top {config.TOP_GAINERS}】股性解剖")
    )
    lines.append("---")
    lines.append("")
    lines.extend(
        _board_table(losers, f"【24h 跌幅榜 Top {config.TOP_LOSERS}】股性解剖")
    )
    lines.append("---")
    lines.append("")

    lines.append("## 完整結構化數據（關鍵欄位總表）")
    lines.append("")
    lines.append("原始完整數值見 `output/coin_personality_summary.csv`。")
    lines.append("")

    key = [
        "board",
        "rank",
        "symbol",
        "price_change_pct_24h",
        "price_position_pct",
        "atr_15m_pct",
        "pulse_index",
        "long_wick_freq",
        "spike_count",
        "cont_rate_5m",
        "rev_rate_5m",
        "streak3_cont_rate",
        "streak3_rev_rate",
        "recent_volume_ratio",
    ]
    key = [c for c in key if c in df.columns]
    ordered = pd.concat([gainers, losers], ignore_index=True)

    lines.append("| " + " | ".join(key) + " |")
    lines.append("| " + " | ".join(["---"] * len(key)) + " |")
    for _, r in ordered.iterrows():
        cells: list[str] = []
        for c in key:
            v = r[c]
            if c == "price_position_pct":
                cells.append(_fmt(v, "pct1"))
            elif c in (
                "price_change_pct_24h",
                "atr_15m_pct",
                "cont_rate_5m",
                "rev_rate_5m",
                "streak3_cont_rate",
                "streak3_rev_rate",
            ):
                cells.append(_fmt(v, "pct"))
            elif c == "long_wick_freq":
                cells.append(_fmt(float(v) * 100.0, "pct"))
            elif c in ("pulse_index", "recent_volume_ratio"):
                cells.append(_fmt(v, "ratio"))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 給 AI 的策略構思 Prompt（請直接複製）")
    lines.append("")
    lines.append("```")
    lines.append(
        "請根據這 20 支當下最熱門小幣的客觀股性數據（24h 漲幅榜 Top 10 + 跌幅榜 Top 10），幫我完成："
    )
    lines.append(
        "1. 將它們分類（例如：暴力脈衝型、高插針洗盤型、爆量延續動能型、爆量騙炮反轉型、連漲/連跌衰竭型、量縮死魚型等）；"
    )
    lines.append("2. 針對不同類型的莊家，設計專屬的進出場策略（觸發條件、方向、持倉時間框架）；")
    lines.append("3. 依各幣的 1m/15m 平均振幅，給出合理的停損、停利參數建議；")
    lines.append(
        "4. 明確標出哪些幣因為量縮、過熱（價格位置接近 100%）、或騙炮率過高，應該直接放棄不做；"
    )
    lines.append("5. 給出今日可執行的優先順序清單（先做哪幾支、為什麼）。")
    lines.append("```")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path
