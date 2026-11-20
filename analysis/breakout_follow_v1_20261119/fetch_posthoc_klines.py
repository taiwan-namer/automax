#!/usr/bin/env python3
"""
POST-HOC reference data only (not a live strategy input).

Fetches finished 1m klines from the public Binance USD-M endpoint
GET /fapi/v1/klines for every symbol archived in the fixed overnight pack,
covering [first archived open_time, pack cutoff]. Uses urllib GET only;
does not import breakout_follow_v1 and touches no account/order path.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PACK = Path("output/breakout_follow_v1/exports/20261119T024357_183e09f4_overnight_pack_20261119T092335")
OUT = Path("analysis/breakout_follow_v1_20261119/posthoc_klines")
BASE = "https://fapi.binance.com/fapi/v1/klines"
CUTOFF_MS = int(datetime.fromisoformat("2026-11-19T09:23:35.913096+08:00").timestamp() * 1000)


def first_open_time(path: Path) -> int:
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        return min(int(row["open_time"]) for row in r)


def fetch(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    rows: list[list] = []
    cur = start_ms
    while cur <= end_ms:
        q = urllib.parse.urlencode(
            {"symbol": symbol, "interval": "1m", "startTime": cur, "endTime": end_ms, "limit": 1500}
        )
        with urllib.request.urlopen(f"{BASE}?{q}", timeout=30) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 60_000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.1)
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {
        "label": "POSTHOC_EXTERNAL_REFERENCE — fetched after the run; NOT the live strategy input",
        "endpoint": BASE,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff_ms": CUTOFF_MS,
        "symbols": {},
    }
    files = sorted((PACK / "klines_1m").glob("*_1m.csv"))
    for p in files:
        sym = p.name[: -len("_1m.csv")]
        start = first_open_time(p)
        try:
            raw = fetch(sym, start, CUTOFF_MS)
            err = None
        except Exception as exc:  # recorded, never silently swallowed
            raw, err = [], repr(exc)
        out = OUT / f"{sym}_1m.csv"
        with out.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades"])
            for x in raw:
                if int(x[6]) >= CUTOFF_MS:
                    continue  # bar not finished by cutoff
                w.writerow([x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8]])
        meta["symbols"][sym] = {
            "rows": sum(1 for x in raw if int(x[6]) < CUTOFF_MS),
            "requested_start_ms": start,
            "error": err,
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        }
        print(sym, meta["symbols"][sym]["rows"], err or "")
    (OUT / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [s for s, m in meta["symbols"].items() if m["error"]]
    print("done", len(files), "errors", bad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
