"""K-line merge / is_closed / closed_klines / decision snapshot immutability."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from breakout_follow_v1 import config
from breakout_follow_v1.market_data import annotate_bar_closed, closed_klines, merge_klines, save_vault, load_vault
from breakout_follow_v1.signals import detect_breakout
from breakout_follow_v1.run_store import RunStore
from tests._isolation import isolate_all_paths, restore_paths


def _bar(ot: int, *, vol: float, h: float, l: float, c: float, o: float | None = None) -> dict:
    return {
        "open_time": ot,
        "open": o if o is not None else c,
        "high": h,
        "low": l,
        "close": c,
        "volume": vol,
        "close_time": ot + 59_999,
        "quote_volume": vol * c,
    }


def test_unclosed_then_closed_updates(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        ot = 1_700_000_000_000
        # fetch 2s into bar → unclosed snapshot
        snap = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=3.0, h=1.0, l=1.0, c=1.0)]), ot + 2_000)
        assert bool(snap.iloc[0]["is_closed"]) is False
        # later fetch after close → full bar
        full = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=100.0, h=1.2, l=0.9, c=1.1)]), ot + 60_000 + 500)
        assert bool(full.iloc[0]["is_closed"]) is True
        merged = merge_klines(snap, full)
        assert len(merged) == 1
        assert float(merged.iloc[0]["volume"]) == 100.0
        assert bool(merged.iloc[0]["is_closed"]) is True
    finally:
        restore_paths(config, saved)


def test_closed_not_overwritten_by_stale_unclosed(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        ot = 1_700_000_060_000
        closed = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=50.0, h=2.0, l=1.0, c=1.5)]), ot + 60_000 + 100)
        stale = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=1.0, h=1.1, l=1.1, c=1.1)]), ot + 1_000)
        merged = merge_klines(closed, stale)
        assert float(merged.iloc[0]["volume"]) == 50.0
        assert bool(merged.iloc[0]["is_closed"]) is True
    finally:
        restore_paths(config, saved)


def test_no_signal_on_unclosed_only(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        # 每根都在收盤前 2 秒抓到 → is_closed=False；牆鐘再晚也不能把舊快照當收盤
        base = 1_700_000_000_000
        frames = []
        for i in range(40):
            ot = base + i * 60_000
            row = _bar(ot, vol=1000.0 if i == 39 else 100.0, h=100.5 if i == 39 else 100.2, l=99.8, c=100.4 if i == 39 else 100.0)
            frames.append(annotate_bar_closed(pd.DataFrame([row]), fetched_at_ms=ot + 2_000))
        df = pd.concat(frames, ignore_index=True)
        assert int(df["is_closed"].astype(bool).sum()) == 0
        now = base + 50 * 60_000
        probe = detect_breakout(df, now_ms=now, book={"bid": 100.3, "ask": 100.4, "mid": 100.35, "spread": 0.001}, data_fetched_at=now / 1000)
        assert probe["signal"] is None
        assert probe["first_reject_code"] == "INSUFFICIENT_BARS"
    finally:
        restore_paths(config, saved)


def test_old_response_cannot_clobber_newer(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        ot = 1_700_000_120_000
        newer = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=80.0, h=3.0, l=2.0, c=2.5)]), ot + 60_000 + 2000)
        older = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=10.0, h=2.1, l=2.0, c=2.05)]), ot + 60_000 + 500)
        # both closed; merge should keep newer fetched_at
        m = merge_klines(newer, older)
        assert float(m.iloc[0]["volume"]) == 80.0
    finally:
        restore_paths(config, saved)


def test_warmup_short_and_time_boundary(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        ot0 = 1_700_000_180_000
        rows = [_bar(ot0 + i * 60_000, vol=10.0, h=1.0, l=0.9, c=0.95) for i in range(5)]
        fetch = ot0 + 5 * 60_000 + 1000
        df = annotate_bar_closed(pd.DataFrame(rows), fetch)
        closed = closed_klines(df, now_ms=fetch)
        assert len(closed) == 5  # all closed at fetch
        probe = detect_breakout(df, now_ms=fetch, book={"bid": 0.95, "ask": 0.96, "mid": 0.955, "spread": 0.01}, data_fetched_at=fetch / 1000)
        assert probe["first_reject_code"] == "INSUFFICIENT_BARS"
    finally:
        restore_paths(config, saved)


def test_decision_snapshot_immutable_after_kline_update(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        store = RunStore(run_id="t_fix_snap", root=config.ROOT_OUT / "runs" / "t_fix_snap")
        store.ensure()
        payload = {"decision_id": "d1", "vol_x": 3.5, "note": "frozen"}
        path = store.save_decision_snapshot("d1", payload)
        assert path.exists()
        # update klines
        ot = 1_700_000_240_000
        df1 = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=1.0, h=1.0, l=1.0, c=1.0)]), ot + 1000)
        store.archive_klines("TESTUSDT", df1, note="partial", fetched_at_ms=ot + 1000)
        df2 = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=99.0, h=1.5, l=0.8, c=1.2)]), ot + 60_000 + 100)
        store.archive_klines("TESTUSDT", df2, note="full", fetched_at_ms=ot + 60_000 + 100)
        # snapshot unchanged
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["vol_x"] == 3.5
        assert loaded["note"] == "frozen"
        # second write same id must not overwrite
        path2 = store.save_decision_snapshot("d1", {"decision_id": "d1", "vol_x": 9.9})
        assert path2 == path
        assert json.loads(path.read_text())["vol_x"] == 3.5
        # archive updated to full
        arch = pd.read_csv(store.kline_dir / "TESTUSDT_1m.csv")
        assert float(arch.iloc[0]["volume"]) == 99.0
    finally:
        restore_paths(config, saved)


def test_vault_save_load_roundtrip(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        ot = 1_700_000_300_000
        df = annotate_bar_closed(pd.DataFrame([_bar(ot, vol=12.0, h=5.0, l=4.0, c=4.5)]), ot + 60_000 + 10)
        save_vault("BTCUSDT", df)
        loaded = load_vault("BTCUSDT")
        assert len(loaded) == 1
        assert bool(loaded.iloc[0]["is_closed"]) is True
        assert str(config.KLINE_VAULT_DIR).endswith("kline_vault_v2_clean") or "kline_vault_v2_clean" in str(config.KLINE_VAULT_DIR)
    finally:
        restore_paths(config, saved)
