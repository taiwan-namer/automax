#!/usr/bin/env python3
"""
幣安 USDT 永續合約・當紅小幣股性解剖 + 策略分派 + 模擬盯盤

使用方式：
    python3 main.py
        → Top20 股性解剖 → AI 報告 → 策略分派 → --once 訊號掃描驗證

    python3 main.py --live
        → 直接啟動 24h 循環盯盤（每 10 分鐘重刷分派 + 每分鐘掃描進出場）
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import config
from fetcher_and_analyzer import run_analysis
from live_paper_trader import run_paper_trader
from report_generator import generate_report
from strategy_router import run_router


def _progress(current: int, total: int, symbol: str, message: str) -> None:
    print(f"[{current}/{total}] {message}")
    sys.stdout.flush()


async def _run_batch_pipeline() -> None:
    """解剖 → 報告 → 分派 → 單次訊號掃描。"""
    print("=" * 64)
    print("當紅小幣股性解剖 + 策略分派 + 單次訊號驗證")
    print(f"K線回看：1m={config.LOOKBACK_1M_HOURS}h / 15m={config.LOOKBACK_15M_DAYS}天")
    print(f"K線金庫：{config.KLINES_VAULT_DIR}/（增量去重覆寫）")
    print(f"成交額下限：{config.MIN_QUOTE_VOLUME_USDT:,.0f} USDT")
    print("=" * 64)
    print()

    csv_path = await run_analysis(progress_callback=_progress)
    print()
    print("-" * 64)
    print(f"特徵摘要已寫入：{csv_path}")

    print("正在產生 AI 報告…")
    md_path = generate_report(csv_path=csv_path)
    print(f"AI 報告已產出：{md_path}")

    print("正在執行策略分派（Strategy Router）…")
    plans, plan_path = run_router(csv_path=csv_path)
    tradeable = sum(1 for p in plans if p.get("tradeable"))
    skipped = sum(1 for p in plans if p.get("status") == "SKIP")
    watch = sum(1 for p in plans if p.get("status") == "WATCH_ONLY")
    print(f"分派完成：可交易 {tradeable}｜觀察 {watch}｜剔除 {skipped}")
    print(f"分派計畫：{plan_path}")

    print()
    print("-" * 64)
    print("啟動 --once 訊號掃描驗證…")
    await run_paper_trader(loop_mode=False)

    print("=" * 64)
    print("批次流程完成。掛機請執行：python3 main.py --live")


def main() -> None:
    parser = argparse.ArgumentParser(description="小幣股性解剖 / 策略分派 / 模擬盯盤")
    parser.add_argument(
        "--live",
        action="store_true",
        help="啟動 24h 循環盯盤（每 10 分鐘更新股性分派 + 每分鐘掃描進出場）",
    )
    args = parser.parse_args()

    if sys.version_info < (3, 11):
        print(
            f"警告：建議使用 Python 3.11+（目前 {sys.version_info.major}.{sys.version_info.minor}），"
            "仍會嘗試執行。",
            file=sys.stderr,
        )

    try:
        if args.live:
            print("=" * 64)
            print("啟動 Live Paper Trader（24h 循環）")
            print(f"榜單重刷：每 {config.REFRESH_ROUTING_MINUTES} 分鐘（K 線增量快取）")
            print("持倉保護：掉榜未平倉 → CLOSE_ONLY 續盯 TP/SL/TIME")
            print("=" * 64)
            asyncio.run(run_paper_trader(loop_mode=True))
        else:
            asyncio.run(_run_batch_pipeline())
    except KeyboardInterrupt:
        print("\n已中斷。輸出檔案已保留於 output/。")
        sys.exit(130)


if __name__ == "__main__":
    main()
