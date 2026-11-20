"""Offline probes for two suspected defects (paper modules only, cwd = test_sandbox/).

P1  portfolio.manage_position: extreme_price update is discarded by the second get_position().
P2  market_data.fetch_klines_incremental merge: concat([cached, fresh]).drop_duplicates("open_time")
    keeps the FIRST (cached, possibly unclosed) version of a bar.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
SB = Path(__file__).resolve().parent / "test_sandbox"
SB.mkdir(exist_ok=True)
os.chdir(SB)
sys.path.insert(0, str(REPO))

from breakout_follow_v1 import config  # noqa: E402
from breakout_follow_v1.account import AccountState  # noqa: E402
from breakout_follow_v1.portfolio import get_position, manage_position, try_open  # noqa: E402
from breakout_follow_v1.util import ensure_dirs, now_iso  # noqa: E402

ensure_dirs()
config.ACCOUNT_PATH = config.ROOT_OUT / "probe_account.json"
out = []

# ---- P1
st = AccountState(run_id="probe", cash_u=10_000, initial_capital_u=10_000)
sig = {"est_fill_price": 100.0, "atr": 0.2, "atr_time": now_iso(), "signal_open_time": 0,
       "range_high": 99.9, "range_low": 99.0, "vol_x": 5}
r = try_open(st, symbol="PROBEUSDT", side="LONG", signal=sig, source_tags=["PROBE"],
             filters={"stepSize": 0.001, "minQty": 0.001, "minNotional": 5})
assert r["ok"], r
p0 = get_position(st, "PROBEUSDT")
out.append(f"P1 open entry={p0.entry_price} init_stop={p0.initial_stop} extreme={p0.extreme_price}")
for px in (100.3, 100.6, 100.9):
    book = {"bid": px, "ask": px + 0.01, "mid": px + 0.005, "spread": 0.0001, "fetched_at": time.time()}
    manage_position(st, "PROBEUSDT", book=book, atr_latest=0.2)
    p = get_position(st, "PROBEUSDT")
    out.append(f"P1 bid={px} -> stored extreme={p.extreme_price} effective_stop={p.effective_stop:.6f} "
               f"(intended trail≈{px*(1-config.SLIPPAGE_ADVERSE) - 1.5*0.2:.6f})")
p = get_position(st, "PROBEUSDT")
out.append("P1 RESULT: " + ("DEFECT CONFIRMED (extreme stuck at entry)" if abs(p.extreme_price - p0.entry_price) < 1e-12
                            else "extreme updates (no defect)"))

# ---- P2  (same expression as market_data.py lines 256-261)
cached = pd.DataFrame([{"open_time": 60_000, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 3.0}])   # seen 2s into bar
fresh = pd.DataFrame([{"open_time": 60_000, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100.0}])  # closed bar
df = pd.concat([cached, fresh], ignore_index=True).drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
out.append(f"P2 merged row: {df.iloc[0].to_dict()}")
out.append("P2 RESULT: " + ("DEFECT CONFIRMED (stale cached row kept)" if df.iloc[0]['volume'] == 3.0 else "fresh row kept"))
print("\n".join(out))
