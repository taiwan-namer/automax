#!/usr/bin/env python3
"""部署後觀察：≥N 個新 1m 收盤邊界。

- 只讀正式乾淨 vault（由 paper 程序寫入）
- 另以公開 GET /fapi/v1/klines 做參考比對（不呼叫 save_vault）
- 不寫帳戶、不下單、不與帳戶競爭
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
sys.path.insert(0, str(REPO))

from breakout_follow_v1 import config
from breakout_follow_v1.market_data import GuardedSession, annotate_bar_closed, load_vault
from breakout_follow_v1.signals import detect_breakout

TZ = timezone(timedelta(hours=8))
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
N_CLOSES = 5


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=TZ).isoformat(timespec="seconds")


async def public_klines(gs: GuardedSession, symbol: str, limit: int = 5) -> pd.DataFrame:
    raw = await gs.get_json("/fapi/v1/klines", params={"symbol": symbol, "interval": "1m", "limit": limit})
    fetched_at_ms = int(time.time() * 1000)
    rows = [
        {
            "open_time": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5]),
            "close_time": int(x[6]),
            "quote_volume": float(x[7]),
        }
        for x in raw
    ]
    return annotate_bar_closed(pd.DataFrame(rows), fetched_at_ms, interval="1m")


async def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/breakout_follow_v1/post_deploy_observe")
    out.mkdir(parents=True, exist_ok=True)
    events: list[dict] = []
    failures: list[dict] = []
    closes_seen = 0
    print(
        f"[{datetime.now(TZ).isoformat(timespec='seconds')}] observe start "
        f"vault={config.KLINE_VAULT_DIR.resolve()} tag={getattr(config,'CODE_FIX_TAG',None)} need={N_CLOSES}"
    )

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        gs = GuardedSession(session)
        while closes_seen < N_CLOSES:
            now_ms = int(time.time() * 1000)
            boundary = (now_ms // 60000) * 60000
            wait = (boundary + 60000 + 2500 - now_ms) / 1000.0
            if wait < 0.5:
                wait += 60
            print(f"[{datetime.now(TZ).isoformat(timespec='seconds')}] waiting {wait:.1f}s (closes_seen={closes_seen})")
            await asyncio.sleep(wait)

            # 給 paper 掃描一點時間寫入 vault（SCAN 約在秒數附近）
            await asyncio.sleep(8)

            boundary_ms = (int(time.time() * 1000) // 60000) * 60000
            just_closed = boundary_ms - 60000

            for sym in SYMBOLS:
                try:
                    ref = await public_klines(gs, sym, limit=5)
                except Exception as e:
                    failures.append({"check": "public_fetch", "symbol": sym, "error": str(e)})
                    print("FETCH_FAIL", sym, e)
                    continue
                ref_closed = ref[ref["is_closed"]]
                ref_last_closed = int(ref_closed.iloc[-1]["open_time"]) if len(ref_closed) else None

                vault = load_vault(sym)
                vault_closed = vault[vault["is_closed"]] if len(vault) and "is_closed" in vault.columns else vault
                vault_last_ot = int(vault.iloc[-1]["open_time"]) if len(vault) else None
                vault_last_closed = bool(vault.iloc[-1]["is_closed"]) if len(vault) else None
                vault_last_closed_ot = int(vault_closed.iloc[-1]["open_time"]) if len(vault_closed) else None

                fetched_at = int(time.time() * 1000)
                book = {"bid": 1.0, "ask": 1.0, "mid": 1.0, "spread": 0.0, "fetched_at": fetched_at / 1000.0}
                probe = detect_breakout(vault, now_ms=fetched_at, book=book, data_fetched_at=fetched_at / 1000.0) if len(vault) else {}
                excl = list(probe.get("excluded_unclosed") or [])
                sig_ot = probe.get("signal_open_time")

                lag_vs_ref = None if (vault_last_closed_ot is None or ref_last_closed is None) else (
                    (ref_last_closed - vault_last_closed_ot) // 60000
                )
                lag_vs_boundary = None if vault_last_closed_ot is None else (just_closed - vault_last_closed_ot) // 60000

                row = {
                    "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                    "symbol": sym,
                    "expected_just_closed_ot": just_closed,
                    "expected_just_closed_iso": iso(just_closed),
                    "ref_last_closed_ot": ref_last_closed,
                    "vault_last_ot": vault_last_ot,
                    "vault_last_is_closed": vault_last_closed,
                    "vault_last_closed_ot": vault_last_closed_ot,
                    "lag_bars_vs_ref": lag_vs_ref,
                    "lag_bars_vs_boundary": lag_vs_boundary,
                    "signal_open_time": sig_ot,
                    "excluded_unclosed": excl,
                    "signal_reject": probe.get("first_reject_code"),
                }
                if lag_vs_ref is not None and lag_vs_ref > 0:
                    failures.append({"check": "vault_lag_vs_public", **row})
                if sig_ot is not None and sig_ot in excl:
                    failures.append({"check": "signal_on_unclosed", **row})
                events.append(row)
                print(
                    f"  {sym} boundary_closed={iso(just_closed)} "
                    f"vault_closed_ot={iso(vault_last_closed_ot) if vault_last_closed_ot else None} "
                    f"lag_ref={lag_vs_ref} excl={excl}"
                )

            # mid-minute：vault 最新若為形成中，不得 is_closed=True；訊號不得用該根
            await asyncio.sleep(15)
            for sym in SYMBOLS:
                vault = load_vault(sym)
                if not len(vault):
                    failures.append({"check": "vault_empty_mid", "symbol": sym})
                    continue
                mid = {
                    "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                    "phase": "mid_minute",
                    "symbol": sym,
                    "vault_last_ot": int(vault.iloc[-1]["open_time"]),
                    "vault_last_is_closed": bool(vault.iloc[-1]["is_closed"]),
                }
                # 若最後一根 open_time == 當前分鐘，必須未收盤
                cur_ot = (int(time.time() * 1000) // 60000) * 60000
                if mid["vault_last_ot"] == cur_ot and mid["vault_last_is_closed"]:
                    failures.append({"check": "forming_marked_closed", **mid})
                events.append(mid)
                print(f"  MID {sym} last_ot={iso(mid['vault_last_ot'])} is_closed={mid['vault_last_is_closed']}")

            closes_seen += 1

    report = {
        "passed": len(failures) == 0,
        "failures": failures,
        "closes": closes_seen,
        "events": events,
        "vault": str(config.KLINE_VAULT_DIR.resolve()),
        "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
        "ended": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = out / "post_deploy_close_observe.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failures_n": len(failures), "closes": closes_seen, "out": str(path)}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
