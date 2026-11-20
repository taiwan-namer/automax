#!/usr/bin/env python3
"""依複合交易識別碼核對孤立 OPEN（不改歷史、不偽造平倉）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FOCUS = ("MMTUSDT", "USUALUSDT")


def entry_ts_of(r: pd.Series) -> str:
    et = r.get("entry_time")
    if pd.notna(et) and str(et) not in ("", "nan", "NaN"):
        return str(et)
    return str(r.get("timestamp"))


def trade_id_key(r: pd.Series) -> tuple:
    # CSV 無獨立 trade_id；以複合鍵作為交易識別碼
    ep = r.get("entry_price")
    ep_v = round(float(ep), 10) if pd.notna(ep) else None
    return (
        str(r["symbol"]),
        entry_ts_of(r),
        str(r["school"]),
        str(r["side"]),
        ep_v,
    )


def main() -> int:
    path = ROOT / "output" / "live_trades_log.csv"
    df = pd.read_csv(path)
    df["event"] = df["event"].astype(str).str.upper()

    opens: dict = {}
    closes: dict = {}
    for _, r in df.iterrows():
        k = trade_id_key(r)
        if r["event"] == "OPEN":
            opens[k] = r
        elif r["event"] == "CLOSE":
            closes.setdefault(k, []).append(r)

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines: list[str] = [
        "# 孤立 OPEN 稽核（orphan_positions_audit）",
        "",
        f"> 產生時間：{now}",
        "> 配對鍵：`(symbol, entry_time, school, side, entry_price)`（CSV 無獨立 trade_id → 複合識別碼）",
        "> **未**偽造平倉、未寫入 SL_HIT/TIME_EXIT、未刪改歷史、未納入勝率",
        "",
        "| 交易識別碼 | symbol | side | school | OPEN時間 | 可配對CLOSE | 狀態 |",
        "|---|---|---|---|---|---|---|",
    ]

    orphan_n = 0
    focus_rows = [(k, o) for k, o in opens.items() if k[0] in FOCUS]
    focus_rows.sort(key=lambda x: x[0][1])

    for k, o in focus_rows:
        paired = closes.get(k) or []
        tid = f"{k[0]}|{k[1]}|{k[2]}|{k[3]}|{k[4]}"
        if paired:
            status = "PAIRED"
            has_close = "Y"
        else:
            status = "ORPHAN_OPEN_UNRESOLVED"
            has_close = "N"
            orphan_n += 1
        open_t = o.get("timestamp") if pd.notna(o.get("timestamp")) else "UNKNOWN"
        lines.append(
            f"| `{tid}` | {k[0]} | {k[3]} | {k[2]} | {open_t} | {has_close} | **{status}** |"
        )

    lines.extend(
        [
            "",
            f"焦點幣孤立 OPEN 數：**{orphan_n}**",
            "",
            "## 孤立明細",
            "",
        ]
    )

    for k, o in focus_rows:
        if closes.get(k):
            continue
        tid = f"{k[0]}|{k[1]}|{k[2]}|{k[3]}|{k[4]}"
        trade_no = o.get("trade_no")
        trade_no_s = str(trade_no) if pd.notna(trade_no) else "UNKNOWN"
        lines.extend(
            [
                f"### `{tid}`",
                "",
                "- 狀態：`ORPHAN_OPEN_UNRESOLVED`",
                f"- symbol：{k[0]}",
                f"- side：{k[3]}",
                f"- school：{k[2]}",
                f"- OPEN時間：{o.get('timestamp', 'UNKNOWN')}",
                f"- entry_price：{o.get('entry_price', 'UNKNOWN')}",
                f"- trade_no：{trade_no_s}",
                f"- mode：{o.get('mode', 'UNKNOWN')}",
                "- 可配對 CLOSE：無",
                "- exit_price / exit_reason / pnl：**UNKNOWN**（禁止回填）",
                "",
            ]
        )

    lines.extend(
        [
            "## 說明",
            "",
            "- 程序中斷後記憶體持倉消失；CSV 殘留 OPEN 不代表目前仍持倉。",
            "- 本稽核僅標記，不產生平倉事件，不把孤立 OPEN 算進勝率。",
            "",
        ]
    )

    out = ROOT / "output" / "orphan_positions_audit.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} orphans={orphan_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
