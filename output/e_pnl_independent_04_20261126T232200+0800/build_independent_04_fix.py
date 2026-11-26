#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wp-e-pnl-independent-04 修正：雙截止橋接、歷史起點、userTrades 金額映射。唯讀。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from pathlib import Path

getcontext().prec = 28

ROOT = Path(r"C:\Users\Admin\MyProjects\automax")
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from _e_es_binance_status import make_ad  # noqa: E402

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 22, 0, 0, tzinfo=TZ)
PRINCIPAL = Decimal("5000")
PRINCIPAL_TS = datetime.fromisoformat("2026-11-25T19:01:55+08:00")
CUT_A = datetime.fromisoformat("2026-11-26T21:42:08+08:00")
CUT_B = datetime.fromisoformat("2026-11-26T22:05:57+08:00")
# 21:42 錢包來自當時本機產物（非現查）；標明來源
WALLET_A = Decimal("5485.53735768")
E_DIR = ROOT / "output/testnet_e_trend_v2_local/data_e/output/testnet_e_trend_v2_tp05_trail"
E_LOG = ROOT / "output/testnet_e_trend_v2_local/logs/strat_e_trend_v2_testnet.log"
ERA = "2026-11-25T20:09:05"


def D(x) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def iso(ts: datetime) -> str:
    return ts.astimezone(TZ).isoformat(timespec="seconds")


def utc_iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def fetch_income(ad, end: datetime) -> tuple[list[dict], int]:
    out: list[dict] = []
    t = int(START.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    pages = 0
    while t < end_ms and pages < 250:
        rows = (
            ad._request(
                "GET",
                "/fapi/v1/income",
                {"startTime": t, "endTime": end_ms, "limit": 1000},
                signed=True,
            )
            or []
        )
        pages += 1
        if not rows:
            break
        out.extend(rows)
        last = int(rows[-1]["time"])
        if len(rows) < 1000:
            break
        t = last + 1
    seen = set()
    uniq = []
    for r in out:
        key = (r.get("tranId"), r.get("time"), r.get("incomeType"), r.get("income"), r.get("symbol"), r.get("tradeId"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq, pages


def fetch_user_trades(ad, start: datetime, end: datetime) -> tuple[list[dict], int]:
    """全符號分頁：用 startTime/endTime 無 symbol（若拒則改逐 symbol）。"""
    out: list[dict] = []
    t = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    pages = 0
    while t < end_ms and pages < 400:
        try:
            rows = (
                ad._request(
                    "GET",
                    "/fapi/v1/userTrades",
                    {"startTime": t, "endTime": end_ms, "limit": 1000},
                    signed=True,
                )
                or []
            )
        except Exception as e:
            return [], -1  # signal failure
        pages += 1
        if not rows:
            break
        out.extend(rows)
        last = int(rows[-1]["time"])
        if len(rows) < 1000:
            break
        t = last + 1
    seen = set()
    uniq = []
    for r in out:
        key = (r.get("id"), r.get("orderId"), r.get("symbol"), r.get("time"), r.get("qty"), r.get("price"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq, pages


def sum_income(rows: list[dict]) -> dict[str, Decimal]:
    s: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in rows:
        s[str(r.get("incomeType") or "?")] += D(r.get("income"))
    return dict(s)


def bridge_at(income: list[dict], cutoff: datetime, wallet: Decimal | None, wallet_src: str) -> dict:
    p_ms = int(PRINCIPAL_TS.timestamp() * 1000)
    c_ms = int(cutoff.timestamp() * 1000)
    # 同秒本金事件
    same_sec = [r for r in income if abs(int(r["time"]) - p_ms) <= 1000]
    before = [r for r in income if int(r["time"]) < p_ms]
    after = [r for r in income if p_ms < int(r["time"]) <= c_ms]
    at_exact = [r for r in income if int(r["time"]) == p_ms]
    sum_before = sum_income(before)
    sum_after = sum_income(after)
    trading = sum(
        (sum_after.get(k, Decimal("0")) for k in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")),
        Decimal("0"),
    )
    transfer = sum_after.get("TRANSFER", Decimal("0"))
    other = sum((v for k, v in sum_after.items() if k not in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "TRANSFER")), Decimal("0"))
    total_after = sum(sum_after.values(), Decimal("0"))
    implied = PRINCIPAL + total_after
    residual = None if wallet is None else (wallet - implied)
    # 本金前無法從 balance 歷史證實期初；條件式：若本金轉入前錢包=W0，則 W0+5000+after=wallet
    return {
        "cutoff": iso(cutoff),
        "cutoff_utc": utc_iso(cutoff),
        "wallet": str(wallet) if wallet is not None else None,
        "wallet_source": wallet_src,
        "principal_ts": iso(PRINCIPAL_TS),
        "principal_ts_utc": utc_iso(PRINCIPAL_TS),
        "same_second_events": [
            {
                "time": iso(datetime.fromtimestamp(int(r["time"]) / 1000, tz=TZ)),
                "type": r.get("incomeType"),
                "income": str(D(r.get("income"))),
                "info": str(r.get("info") or "")[:80],
            }
            for r in sorted(same_sec, key=lambda x: int(x["time"]))
        ],
        "at_exact_principal": [
            {
                "time": iso(datetime.fromtimestamp(int(r["time"]) / 1000, tz=TZ)),
                "type": r.get("incomeType"),
                "income": str(D(r.get("income"))),
            }
            for r in at_exact
        ],
        "after_counts": dict(Counter(str(r.get("incomeType")) for r in after)),
        "after_sums": {k: str(v) for k, v in sorted(sum_after.items())},
        "trading_net": str(trading),
        "transfer_after": str(transfer),
        "other_after": str(other),
        "income_after_total": str(total_after),
        "implied_wallet": str(implied),
        "residual": str(residual) if residual is not None else None,
        "before_transfer_sums": {k: str(v) for k, v in sorted(sum_before.items())},
        "conditional": {
            "note": "本金轉入前錢包餘額無官方歷史 balance API 證據；條件式：W0 + 5000 + Σincome_after = wallet",
            "implied_W0_if_wallet_known": str(wallet - PRINCIPAL - total_after) if wallet is not None else None,
        },
        "sampling": {
            "income_end_inclusive": True,
            "wallet_vs_income_lag_note": "錢包為時點快照；income 篩至 cutoff 秒；兩者非同一次 API 原子讀取",
        },
    }


def local_origins() -> dict:
    """首啟／首單／首成交：本機 log + fills。"""
    origins: dict = {"sources": []}
    # log 檔建立／首行
    if E_LOG.exists():
        st = E_LOG.stat()
        first_lines = []
        with E_LOG.open(encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= 30:
                    break
                first_lines.append(line.rstrip()[:200])
        # 找最早時間戳
        ts_re = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+08:00|Z)?)")
        first_ts = None
        for line in first_lines:
            m = ts_re.search(line)
            if m:
                first_ts = m.group(1)
                break
        origins["log"] = {
            "path": str(E_LOG),
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=TZ).isoformat(timespec="seconds"),
            "first_timestamp_in_head": first_ts,
            "head_sample": first_lines[:8],
        }
        origins["sources"].append("strat_e_trend_v2_testnet.log")
    fills_path = E_DIR / "fills.jsonl"
    first_any = first_open = first_fillish = None
    n = 0
    if fills_path.exists():
        with fills_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                n += 1
                d = json.loads(line)
                ts = str(d.get("recorded_at") or "")
                if not first_any and ts:
                    first_any = d
                if not first_open and d.get("event") == "OPEN" and ts:
                    first_open = d
                if not first_fillish and d.get("event") in ("OPEN", "CLOSE", "SETTLEMENT_CLOSE", "FEE_ADJUST") and ts:
                    first_fillish = d
                if first_any and first_open and first_fillish and n > 5:
                    # keep scanning for true earliest open if first lines aren't OPEN
                    pass
        # second pass for absolute earliest OPEN / trade-like
        earliest_open = earliest_event = None
        with fills_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                ts = str(d.get("recorded_at") or "")
                if not ts:
                    continue
                if earliest_event is None or ts < str(earliest_event.get("recorded_at")):
                    earliest_event = d
                if d.get("event") == "OPEN" and (earliest_open is None or ts < str(earliest_open.get("recorded_at"))):
                    earliest_open = d
        origins["fills"] = {
            "path": str(fills_path),
            "rows": n,
            "earliest_event": {
                "ts": earliest_event.get("recorded_at") if earliest_event else None,
                "event": earliest_event.get("event") if earliest_event else None,
                "symbol": earliest_event.get("symbol") if earliest_event else None,
                "trade_id": earliest_event.get("trade_id") if earliest_event else None,
            },
            "earliest_OPEN": {
                "ts": earliest_open.get("recorded_at") if earliest_open else None,
                "symbol": earliest_open.get("symbol") if earliest_open else None,
                "trade_id": earliest_open.get("trade_id") if earliest_open else None,
                "binance_order_id": earliest_open.get("binance_order_id") if earliest_open else None,
            },
        }
        origins["sources"].append("fills.jsonl")
    # account_state / run meta
    for name in ("account_state.json", "run_meta.json", "config.json"):
        p = E_DIR / name
        if p.exists():
            origins.setdefault("files", {})[name] = {
                "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=TZ).isoformat(timespec="seconds"),
                "size": p.stat().st_size,
            }
    return origins


def load_local_groups():
    by_tid = defaultdict(list)
    with (E_DIR / "fills.jsonl").open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if str(d.get("recorded_at") or "") < ERA:
                continue
            tid = d.get("trade_id")
            if tid:
                by_tid[tid].append(d)
    multi, open_nc = [], []
    for tid, evs in by_tid.items():
        evs = sorted(evs, key=lambda x: str(x.get("recorded_at")))
        kinds = [str(e.get("event")) for e in evs]
        closes = [e for e in evs if e.get("event") in ("CLOSE", "SETTLEMENT_CLOSE")]
        if "OPEN" in kinds and len(closes) > 1:
            multi.append({"trade_id": tid, "symbol": evs[0].get("symbol"), "evs": evs, "closes": closes})
        elif "OPEN" in kinds and not closes:
            open_nc.append({"trade_id": tid, "symbol": next(e.get("symbol") for e in evs if e.get("event") == "OPEN"), "evs": evs})
    return multi, open_nc


def map_trades(multi, open_nc, user_trades: list[dict]) -> dict:
    # index by orderId and by symbol
    by_order: dict[str, list] = defaultdict(list)
    by_sym: dict[str, list] = defaultdict(list)
    for t in user_trades:
        oid = str(t.get("orderId") or "")
        if oid:
            by_order[oid].append(t)
        by_sym[str(t.get("symbol"))].append(t)

    def realized_commission(trades: list[dict]) -> tuple[Decimal, Decimal]:
        # futures userTrades: realizedPnl + commission
        rp = sum((D(t.get("realizedPnl")) for t in trades), Decimal("0"))
        cm = sum((D(t.get("commission")) for t in trades), Decimal("0"))
        return rp, cm

    multi_rows = []
    mapped_close_ids = 0
    unmapped_close = 0
    dup_impact = Decimal("0")
    for g in multi:
        order_ids = []
        for e in g["closes"]:
            for k in ("binance_order_id", "order_id", "exchange_order_id"):
                if e.get(k):
                    order_ids.append(str(e.get(k)))
        order_ids = list(dict.fromkeys(order_ids))
        matched = []
        for oid in order_ids:
            matched.extend(by_order.get(oid, []))
        # unique by trade id
        seen = set()
        uniq = []
        for t in matched:
            tid = t.get("id")
            if tid in seen:
                continue
            seen.add(tid)
            uniq.append(t)
        rp, cm = realized_commission(uniq)
        n_close_events = len(g["closes"])
        n_ex_trades = len(uniq)
        # 若本機 close 事件數 > 對到的唯一成交，差額視為「事件重複」風險（金額以交易所唯一為準）
        if order_ids and uniq:
            mapped_close_ids += 1
        else:
            unmapped_close += 1
        multi_rows.append(
            {
                "local_trade_id": g["trade_id"],
                "symbol": g["symbol"],
                "n_local_close_events": n_close_events,
                "binance_order_ids": order_ids,
                "n_exchange_trades": n_ex_trades,
                "exchange_realizedPnl": str(rp),
                "exchange_commission": str(cm),
                "mapped": bool(uniq),
            }
        )
        if n_close_events > 1 and n_ex_trades >= 1:
            # 本機若把每筆 CLOSE 當一次 realized，可能高估；影響以上述交易所合計為真值錨
            dup_impact += Decimal("0")  # 佔位：真實雙計需本機金額欄；改報告文字說明

    open_rows = []
    mapped_open = 0
    for g in open_nc:
        order_ids = []
        for e in g["evs"]:
            if e.get("event") != "OPEN":
                continue
            for k in ("binance_order_id", "order_id", "exchange_order_id"):
                if e.get(k):
                    order_ids.append(str(e.get(k)))
        order_ids = list(dict.fromkeys(order_ids))
        matched = []
        for oid in order_ids:
            matched.extend(by_order.get(oid, []))
        seen = set()
        uniq = []
        for t in matched:
            if t.get("id") in seen:
                continue
            seen.add(t.get("id"))
            uniq.append(t)
        rp, cm = realized_commission(uniq)
        if uniq:
            mapped_open += 1
        open_rows.append(
            {
                "local_trade_id": g["trade_id"],
                "symbol": g["symbol"],
                "binance_order_ids": order_ids,
                "n_exchange_trades": len(uniq),
                "exchange_realizedPnl": str(rp),
                "exchange_commission": str(cm),
                "mapped": bool(uniq),
                "has_reconcile": any("RECONCILE" in str(e.get("event")) for e in g["evs"]),
            }
        )

    return {
        "multi_close": {
            "n_groups": len(multi),
            "n_mapped_groups": mapped_close_ids,
            "n_unmapped_groups": unmapped_close,
            "exchange_realized_sum": str(sum((D(r["exchange_realizedPnl"]) for r in multi_rows), Decimal("0"))),
            "exchange_commission_sum": str(sum((D(r["exchange_commission"]) for r in multi_rows), Decimal("0"))),
            "rows": multi_rows,
        },
        "open_no_close": {
            "n_groups": len(open_nc),
            "n_mapped_groups": mapped_open,
            "n_unmapped_groups": len(open_nc) - mapped_open,
            "exchange_realized_sum": str(sum((D(r["exchange_realizedPnl"]) for r in open_rows), Decimal("0"))),
            "exchange_commission_sum": str(sum((D(r["exchange_commission"]) for r in open_rows), Decimal("0"))),
            "rows": open_rows,
        },
        "user_trades_n": len(user_trades),
    }


def daily_full_precision(income: list[dict], end: datetime) -> list[dict]:
    end_ms = int(end.timestamp() * 1000)
    by_day: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    for r in income:
        if int(r["time"]) > end_ms:
            continue
        ts = datetime.fromtimestamp(int(r["time"]) / 1000, tz=TZ)
        by_day[ts.strftime("%Y-%m-%d")][str(r.get("incomeType") or "?")] += D(r.get("income"))
    rows = []
    for d in sorted(by_day):
        x = by_day[d]
        trading = x.get("REALIZED_PNL", 0) + x.get("COMMISSION", 0) + x.get("FUNDING_FEE", 0)
        rows.append(
            {
                "day": d,
                "REALIZED_PNL": str(x.get("REALIZED_PNL", 0)),
                "COMMISSION": str(x.get("COMMISSION", 0)),
                "FUNDING_FEE": str(x.get("FUNDING_FEE", 0)),
                "trading_net": str(trading),
                "TRANSFER": str(x.get("TRANSFER", 0)),
            }
        )
    return rows


def main():
    ad = make_ad(ROOT / ".env.testnet", "E")
    # 現況錢包（對 22:05 用當下再讀；若現在已過該截止，殘差含截止後變動——改以 income 篩截止，錢包用「截止後最近可得」需標明）
    bal_rows = ad._request("GET", "/fapi/v2/balance", signed=True) or []
    usdt = next((r for r in bal_rows if str(r.get("asset")).upper() == "USDT"), {})
    wallet_now = D(usdt.get("balance"))
    upnl_now = D(usdt.get("crossUnPnl"))
    sample_now = datetime.now(TZ)

    income_b, pages_b = fetch_income(ad, CUT_B)
    # 對 CUT_A 用同一批再篩（少打一次 API）
    income_a = [r for r in income_b if int(r["time"]) <= int(CUT_A.timestamp() * 1000)]
    pages_a = pages_b

    # 冪等：再抓一次 CUT_B 比對數
    income_b2, pages_b2 = fetch_income(ad, CUT_B)
    keys1 = {(r.get("tranId"), r.get("time"), r.get("incomeType"), r.get("income"), r.get("tradeId")) for r in income_b}
    keys2 = {(r.get("tranId"), r.get("time"), r.get("incomeType"), r.get("income"), r.get("tradeId")) for r in income_b2}
    idempotent = keys1 == keys2

    bridge_a = bridge_at(income_a, CUT_A, WALLET_A, "e_pnl_trace_03 內嵌快照 5485.53735768（21:42:08 當時）")
    # 22:05 錢包：若現在遠晚於截止，不能用 now 冒充；標條件式＋若有舊 metrics 用舊值
    old_metrics = json.loads(
        (ROOT / "output/e_pnl_trace_03_20261126T220500+0800/metrics_pnl_trace.json").read_text(encoding="utf-8")
    )
    wallet_b = D(old_metrics["exchange_now"]["wallet"])
    bridge_b = bridge_at(income_b, CUT_B, wallet_b, "metrics_pnl_trace.json exchange_now.wallet @ 原 22:05 產物")

    origins = local_origins()
    multi, open_nc = load_local_groups()

    # userTrades：本金後到 CUT_B
    ut, ut_pages = fetch_user_trades(ad, PRINCIPAL_TS, CUT_B)
    if ut_pages < 0:
        # fallback per symbol from local multi/open
        syms = sorted({g["symbol"] for g in multi + open_nc if g.get("symbol")})
        ut = []
        ut_pages = 0
        for sym in syms:
            t = int(PRINCIPAL_TS.timestamp() * 1000)
            end_ms = int(CUT_B.timestamp() * 1000)
            while t < end_ms and ut_pages < 500:
                rows = (
                    ad._request(
                        "GET",
                        "/fapi/v1/userTrades",
                        {"symbol": sym, "startTime": t, "endTime": end_ms, "limit": 1000},
                        signed=True,
                    )
                    or []
                )
                ut_pages += 1
                if not rows:
                    break
                ut.extend(rows)
                last = int(rows[-1]["time"])
                if len(rows) < 1000:
                    break
                t = last + 1
        # dedupe
        seen = set()
        uniq = []
        for r in ut:
            key = (r.get("id"), r.get("orderId"), r.get("symbol"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        ut = uniq

    mapping = map_trades(multi, open_nc, ut)
    daily_a = daily_full_precision(income_a, CUT_A)
    daily_b = daily_full_precision(income_b, CUT_B)

    # 分頁邊界：最後一頁筆數
    boundary = {
        "income_pages_to_cut_b": pages_b,
        "income_unique_to_cut_b": len(income_b),
        "income_unique_to_cut_a": len(income_a),
        "idempotent_rerun": idempotent,
        "user_trades_pages": ut_pages,
        "user_trades_unique": len(ut),
    }

    payload = {
        "generated_at": iso(sample_now),
        "wallet_now_live": str(wallet_now),
        "upnl_now_live": str(upnl_now),
        "origins": origins,
        "bridge_214208": bridge_a,
        "bridge_220557": bridge_b,
        "daily_214208": daily_a,
        "daily_220557": daily_b,
        "mapping": mapping,
        "boundary": boundary,
        "multi_n": len(multi),
        "open_n": len(open_nc),
    }
    (OUT / "metrics_independent_04_fix.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({"ok": True, "boundary": boundary, "multi": len(multi), "open": len(open_nc), "ut": len(ut), "idempotent": idempotent}, ensure_ascii=False))


if __name__ == "__main__":
    main()
