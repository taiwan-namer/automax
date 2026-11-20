"""Reproduce P0 defects on LIVE package vs FIX package (offline, tmp only)."""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent


def probe_extreme(mod_portfolio, mod_account, mod_config, label: str) -> str:
    st = mod_account.AccountState(run_id="probe", cash_u=10_000, initial_capital_u=10_000)
    sig = {
        "est_fill_price": 100.0,
        "atr": 0.2,
        "atr_time": "t",
        "signal_open_time": 0,
        "range_high": 99.9,
        "range_low": 99.0,
        "vol_x": 5,
    }
    r = mod_portfolio.try_open(
        st,
        symbol="PROBEUSDT",
        side="LONG",
        signal=sig,
        source_tags=["PROBE"],
        filters={"stepSize": 0.001, "minQty": 0.001, "minNotional": 5},
    )
    assert r["ok"], r
    p0 = mod_account.get_position(st, "PROBEUSDT")
    for px in (100.3, 100.6, 100.9):
        book = {"bid": px, "ask": px + 0.01, "mid": px + 0.005, "spread": 0.0001, "fetched_at": time.time()}
        mod_portfolio.manage_position(st, "PROBEUSDT", book=book, atr_latest=0.2)
    p = mod_account.get_position(st, "PROBEUSDT")
    stuck = abs(p.extreme_price - p0.entry_price) < 1e-12
    return f"{label}: extreme={p.extreme_price} entry={p0.entry_price} → {'DEFECT' if stuck else 'FIXED'}"


def probe_merge(mod_md, label: str) -> str:
    cached = pd.DataFrame([{"open_time": 60_000, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 3.0, "close_time": 119_999}])
    fresh = pd.DataFrame([{"open_time": 60_000, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100.0, "close_time": 119_999}])
    if hasattr(mod_md, "merge_klines"):
        c = mod_md.annotate_bar_closed(cached, 62_000)
        f = mod_md.annotate_bar_closed(fresh, 120_500)
        df = mod_md.merge_klines(c, f)
        vol = float(df.iloc[0]["volume"])
    else:
        df = pd.concat([cached, fresh]).drop_duplicates(subset=["open_time"])
        vol = float(df.iloc[0]["volume"])
    return f"{label}: merged_volume={vol} → {'DEFECT' if vol == 3.0 else 'FIXED'}"


def main():
    lines = []
    # LIVE package (running source)
    sys.path.insert(0, str(REPO))
    # ensure we import LIVE not FIX
    for name in list(sys.modules):
        if name.startswith("breakout_follow_v1"):
            del sys.modules[name]
    live_md = importlib.import_module("breakout_follow_v1.market_data")
    live_pf = importlib.import_module("breakout_follow_v1.portfolio")
    live_ac = importlib.import_module("breakout_follow_v1.account")
    live_cfg = importlib.import_module("breakout_follow_v1.config")
    # isolate live portfolio account writes into /tmp via monkeypatch
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="bfv1_probe_live_"))
    live_cfg.ACCOUNT_PATH = td / "account.json"
    live_cfg.FILLS_PATH = td / "fills.jsonl"
    live_cfg.TRADES_PATH = td / "trades.csv"
    live_cfg.ROOT_OUT = td
    lines.append(probe_merge(live_md, "LIVE"))
    lines.append(probe_extreme(live_pf, live_ac, live_cfg, "LIVE"))

    # FIX package
    for name in list(sys.modules):
        if name.startswith("breakout_follow_v1"):
            del sys.modules[name]
    sys.path.insert(0, str(FIX))
    fix_md = importlib.import_module("breakout_follow_v1.market_data")
    fix_pf = importlib.import_module("breakout_follow_v1.portfolio")
    fix_ac = importlib.import_module("breakout_follow_v1.account")
    fix_cfg = importlib.import_module("breakout_follow_v1.config")
    td2 = Path(tempfile.mkdtemp(prefix="bfv1_probe_fix_"))
    fix_cfg.ACCOUNT_PATH = td2 / "account.json"
    fix_cfg.FILLS_PATH = td2 / "fills.jsonl"
    fix_cfg.TRADES_PATH = td2 / "trades.csv"
    fix_cfg.ROOT_OUT = td2
    lines.append(probe_merge(fix_md, "FIX"))
    lines.append(probe_extreme(fix_pf, fix_ac, fix_cfg, "FIX"))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
