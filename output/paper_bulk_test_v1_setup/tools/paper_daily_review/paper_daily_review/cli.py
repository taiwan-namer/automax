from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .timeutil import (
    REPORT_HOUR,
    REPORT_MINUTE,
    TAIPEI,
    last_settled_day,
    parse_iso,
)
from .report import generate_day_report


def _default_setup() -> Path:
    # tools/paper_daily_review/ -> setup root
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="唯讀紙上交易每日復盤（不修改策略／帳戶／金庫；不呼叫交易所）"
    )
    p.add_argument(
        "--setup-root",
        type=Path,
        default=_default_setup(),
        help="paper_bulk_test_v1_setup 根目錄",
    )
    p.add_argument(
        "--profile",
        type=str,
        default="paper_bulk_test_v1",
        help=(
            "帳戶／報告 profile。"
            "paper_bulk_test_v1 → analysis_packs/daily_reports/；"
            "其他（如 paper_reverse_fixed_exit_v1）→ analysis_packs/daily_reports_<profile>/。"
            "禁止混算。"
        ),
    )
    p.add_argument(
        "--day",
        type=str,
        default=None,
        help="YYYY-MM-DD（Asia/Taipei 交易日標籤；該日結束於當日 20:15）。mode=yesterday 時可省略",
    )
    p.add_argument(
        "--asof",
        type=str,
        default=None,
        help="截止時間 ISO；預設現在（Taipei）",
    )
    p.add_argument(
        "--mode",
        choices=["yesterday", "day", "backfill"],
        default="yesterday",
        help="yesterday=最近已結算日（20:15 後為當日）；day=指定 --day；backfill=補缺日至最近已結算日",
    )
    p.add_argument(
        "--cutoff-hour",
        type=int,
        default=REPORT_HOUR,
        help="報告排程小時（預設 20=報告 20:25；結算界另為 20:15）",
    )
    p.add_argument(
        "--cutoff-minute",
        type=int,
        default=REPORT_MINUTE,
    )
    args = p.parse_args(argv)

    setup = args.setup_root.resolve()
    asof = parse_iso(args.asof) if args.asof else datetime.now(TAIPEI)
    if asof is None:
        print("invalid --asof", file=sys.stderr)
        return 2

    profile = str(args.profile or "paper_bulk_test_v1").strip()

    if args.mode == "backfill":
        end_day = last_settled_day(asof)
        start_day = end_day - timedelta(days=13)
        if profile == "paper_bulk_test_v1":
            daily = setup / "analysis_packs" / "daily_reports"
        else:
            daily = setup / "analysis_packs" / f"daily_reports_{profile}"
        d = start_day
        results = []
        while d <= end_day:
            if not (daily / d.isoformat() / "manifest.json").exists():
                results.append(generate_day_report(setup, d, asof=asof, profile_id=profile))
            d += timedelta(days=1)
        for r in results:
            print(r["out_dir"], r["completeness"]["status"])
        if not results:
            print("no missing days to backfill")
        return 0

    if args.mode == "day":
        if not args.day:
            print("--day required for mode=day", file=sys.stderr)
            return 2
        day = date.fromisoformat(args.day)
    else:
        day = last_settled_day(asof)
        if args.day:
            day = date.fromisoformat(args.day)

    result = generate_day_report(
        setup,
        day,
        asof=asof,
        cutoff_hour=args.cutoff_hour,
        cutoff_minute=args.cutoff_minute,
        profile_id=profile,
    )
    print(result["summary_path"])
    print(result["completeness"]["status"], f"zh_chars={result['zh_chars']}")
    print(f"profile={profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
