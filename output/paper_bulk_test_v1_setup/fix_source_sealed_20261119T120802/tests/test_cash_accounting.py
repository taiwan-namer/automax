"""Cash/gross accounting + migration dry-run / idempotence."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from breakout_follow_v1 import config
from breakout_follow_v1.account import (
    AccountState,
    get_position,
    migrate_cash_gross_v1,
    save_account,
)
from breakout_follow_v1.portfolio import close_qty, manage_position, try_open
from breakout_follow_v1.util import now_iso
from tests._isolation import isolate_all_paths, restore_paths

REPO = Path(__file__).resolve().parents[3]


def _sig(atr=0.4, entry=100.0, sot=1):
    return {
        "est_fill_price": entry,
        "atr": atr,
        "atr_time": now_iso(),
        "signal_open_time": sot,
        "range_high": entry - 0.1,
        "range_low": entry - 1.0,
        "vol_x": 5.0,
    }


def _filt():
    return {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5}


def _book(mid, ts=None):
    half = mid * 0.0002
    return {"bid": mid - half, "ask": mid + half, "mid": mid, "spread": 2 * half / mid, "fetched_at": ts or time.time()}


def test_definitions_documented():
    """現有定義（修復後）：OPEN cash-=margin+fee；CLOSE cash+=gross-fee；flat 退 margin。"""
    assert config.MARGIN_PER_TRADE_USDT == 1000.0
    assert config.INITIAL_CAPITAL_USDT == 10000.0
    assert config.ACCOUNTING_MIGRATION_VERSION == "cash_gross_v1"


def test_profit_close_cash_equals_equity(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        try_open(st, symbol="PUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())
        pos = get_position(st, "PUSDT")
        # profitable exit
        exit_px = pos.entry_price * 1.01
        close_qty(st, "PUSDT", qty=pos.qty_remaining, exit_price=exit_px, reason="TEST", book_ts=time.time())
        assert not st.positions
        assert abs(st.cash_u - (st.initial_capital_u + st.realized_net_u)) < 1e-6
        assert abs(st.cash_u - st.equity_mark(0.0)) < 1e-6
    finally:
        restore_paths(config, saved)


def test_loss_close_cash_equals_equity(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        try_open(st, symbol="LUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())
        pos = get_position(st, "LUSDT")
        exit_px = pos.entry_price * 0.99
        close_qty(st, "LUSDT", qty=pos.qty_remaining, exit_price=exit_px, reason="TEST", book_ts=time.time())
        assert abs(st.cash_u - st.equity_mark(0.0)) < 1e-6
        assert st.realized_net_u < 0
    finally:
        restore_paths(config, saved)


def test_partial_then_full_fee_gross_once(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        try_open(st, symbol="AAAUSDT", side="LONG", signal=_sig(atr=0.4), source_tags=["T"], filters=_filt())
        fees_open = st.realized_fees_u
        manage_position(st, "AAAUSDT", book=_book(103.5), atr_latest=0.4)
        pos = get_position(st, "AAAUSDT")
        assert pos and pos.partial_tp_done
        # margin still held full until flat (policy unchanged)
        assert abs(st.used_margin() - config.MARGIN_PER_TRADE_USDT) < 1e-9
        close_qty(st, "AAAUSDT", qty=pos.qty_remaining, exit_price=103.5, reason="TEST", book_ts=time.time())
        assert not st.positions
        assert abs(st.cash_u - st.equity_mark(0.0)) < 1e-6
        # fees = open + partial exit + final exit (each once)
        assert st.realized_fees_u > fees_open
        fills = [json.loads(l) for l in config.FILLS_PATH.read_text().splitlines() if l.strip()]
        assert sum(1 for f in fills if f["event"] == "OPEN") == 1
        assert sum(1 for f in fills if f["event"] == "CLOSE_PARTIAL") == 1
        assert sum(1 for f in fills if f["event"] == "CLOSE") == 1
    finally:
        restore_paths(config, saved)


def test_migration_dry_run_target_9668_and_idempotent(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        # Use sealed overnight pack account snapshot (not live mutating account)
        pack_acct = REPO / "output/breakout_follow_v1/exports/20261119T024357_183e09f4_overnight_pack_20261119T092335/trading/account_state.json"
        raw = pack_acct.read_bytes()
        src_hash = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw.decode())
        st = AccountState.from_dict(data)
        # expected from report: flat → 10000 + realized ≈ 9668.7822
        expected = st.initial_capital_u + st.realized_net_u - st.used_margin()
        assert abs(expected - 9668.782178357955) < 0.01
        dry = migrate_cash_gross_v1(st, source_hash=src_hash, reason="dry_run_pack_cutoff", dry_run=True)
        assert dry["applied"] is False
        assert abs(dry["cash_after"] - expected) < 1e-6
        # apply on COPY only
        apply1 = migrate_cash_gross_v1(st, source_hash=src_hash, reason="apply_copy", dry_run=False)
        assert apply1["applied"] is True
        assert abs(st.cash_u - expected) < 1e-6
        apply2 = migrate_cash_gross_v1(st, source_hash=src_hash, reason="apply_again", dry_run=False)
        assert apply2["applied"] is False
        assert apply2["already_applied"] is True
        assert abs(st.cash_u - expected) < 1e-6
        # write only to tmp — never touch live
        save_account(st)
        assert str(config.ACCOUNT_PATH).startswith(str(tmp_path))
    finally:
        restore_paths(config, saved)


def test_duplicate_signal_rejected(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=10_000, initial_capital_u=10_000)
        assert try_open(st, symbol="DUSDT", side="LONG", signal=_sig(sot=42), source_tags=["T"], filters=_filt())["ok"]
        # flatten
        pos = get_position(st, "DUSDT")
        close_qty(st, "DUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="TEST", book_ts=time.time())
        # same signal key must not reopen
        r2 = try_open(st, symbol="DUSDT", side="LONG", signal=_sig(sot=42), source_tags=["T"], filters=_filt(), bypass_cooldown=True)
        assert r2["ok"] is False and r2["block_reason"] == "DUPLICATE_SIGNAL"
    finally:
        restore_paths(config, saved)


def test_live_fills_trades_and_sources_stable_despite_running_process():
    """正式 PID 可能仍在寫 account/vault；測試不得改 fills/trades/原始碼。帳務變動若發生須歸因於正式程序。"""
    before = json.loads((REPO / "worktrees/breakout_follow_v1_fix_20261119/LIVE_DATA_HASHES_BEFORE.json").read_text())
    src = json.loads((REPO / "worktrees/breakout_follow_v1_fix_20261119/LIVE_SOURCE_HASHES_BEFORE.json").read_text())

    def h(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()

    assert h(REPO / "output/breakout_follow_v1/fills.jsonl") == before["fills"]
    assert h(REPO / "output/breakout_follow_v1/trades.csv") == before["trades"]
    for row in src:
        assert h(REPO / "breakout_follow_v1" / row["file"]) == row["live_sha256"], row["file"]
    # 路徑隔離證據：測試寫入皆在 tmp（由其他用例覆蓋）；此處記錄 live account 可能被 PID 更新
    live_acct_now = h(REPO / "output/breakout_follow_v1/account_state.json")
    if live_acct_now != before["account_state"]:
        # 允許正式程序自然更新；禁止測試寫入的證明是 fills/trades/源碼不變 + 隔離路徑測試
        assert before.get("pid") is not None
