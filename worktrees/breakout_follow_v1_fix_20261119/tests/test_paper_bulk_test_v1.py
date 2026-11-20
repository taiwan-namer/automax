"""paper_bulk_test_v1 profile：資金／路徑隔離與持倉上限；不改訊號門檻。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

from breakout_follow_v1 import config
from breakout_follow_v1.account import (
    AccountState,
    ensure_day_rollover,
    get_position,
    load_or_create_account,
    save_account,
)
from breakout_follow_v1.paper_main import _find_conflicting_processes, _profile_from_cmdline
from breakout_follow_v1.portfolio import close_qty, try_open
from breakout_follow_v1.review import write_review
from breakout_follow_v1.safety import LiveTradingForbidden, assert_public_url
from breakout_follow_v1.util import now_iso, today_key
from tests._isolation import isolate_all_paths, restore_paths


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


def _apply_bulk_isolated(tmp: Path):
    saved_paths = isolate_all_paths(config, tmp)
    # isolate 設 RUNTIME_ROOT=tmp 後套 bulk；路徑由 apply_profile 重綁為絕對
    applied = config.apply_profile("paper_bulk_test_v1")
    root = Path(config.ROOT_OUT)
    vault = Path(config.KLINE_VAULT_DIR)
    root.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    Path(config.DECISION_SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    assert root.name == "paper_bulk_test_v1"
    assert Path(config.LOCK_PATH).parent == root
    return saved_paths, applied, root


def _restore_default(saved_paths):
    # 先恢復路徑再回復預設 profile 數值
    restore_paths(config, saved_paths)
    config.apply_profile("breakout_follow_v1")


def test_bulk_first_capital_50000_restart_no_reset(tmp_path):
    saved, applied, root = _apply_bulk_isolated(tmp_path)
    try:
        assert config.INITIAL_CAPITAL_USDT == 50_000.0
        assert config.MAX_CONCURRENT_POSITIONS == 20
        assert config.DAILY_LOSS_LOCK_USDT == 2000.0
        assert "paper_bulk_test_v1" in str(config.ACCOUNT_PATH)
        st = load_or_create_account()
        assert abs(st.initial_capital_u - 50_000.0) < 1e-9
        assert abs(st.cash_u - 50_000.0) < 1e-9
        st.realized_net_u = -12.5
        st.cash_u = 50_000.0 + st.realized_net_u
        save_account(st)
        st2 = load_or_create_account()
        assert abs(st2.initial_capital_u - 50_000.0) < 1e-9
        assert abs(st2.realized_net_u - (-12.5)) < 1e-9
        assert abs(st2.cash_u - (50_000.0 - 12.5)) < 1e-9
        # 不得再次補到 50000
        assert abs(st2.cash_u - 50_000.0) > 1.0
    finally:
        _restore_default(saved)


def test_bulk_can_open_more_than_3_up_to_20(tmp_path):
    saved, _, _ = _apply_bulk_isolated(tmp_path)
    try:
        st = AccountState(
            run_id="t",
            profile_id="paper_bulk_test_v1",
            cash_u=50_000,
            initial_capital_u=50_000,
            day_key=today_key(),
            day_start_equity_u=50_000,
        )
        opened = 0
        for i in range(20):
            sym = f"S{i:02d}USDT"
            r = try_open(st, symbol=sym, side="LONG", signal=_sig(sot=1000 + i), source_tags=["T"], filters=_filt())
            assert r["ok"], r
            opened += 1
        assert opened == 20
        assert len(st.positions) == 20
        # 第 21 倉拒絕
        r21 = try_open(st, symbol="S20USDT", side="LONG", signal=_sig(sot=2000), source_tags=["T"], filters=_filt())
        assert r21["ok"] is False and r21["block_reason"] == "MAX_POSITIONS"
        assert "MAX_POSITIONS" in r21["block_reasons"]
        # 釋放一倉後可再開
        p = get_position(st, "S00USDT")
        close_qty(st, "S00USDT", qty=p.qty_remaining, exit_price=p.entry_price, reason="T", book_ts=time.time())
        r_new = try_open(st, symbol="NEWUSDT", side="LONG", signal=_sig(sot=3000), source_tags=["T"], filters=_filt())
        assert r_new["ok"], r_new
    finally:
        _restore_default(saved)


def test_bulk_same_symbol_and_opposite_blocked(tmp_path):
    saved, _, _ = _apply_bulk_isolated(tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=50_000, initial_capital_u=50_000, day_key=today_key(), day_start_equity_u=50_000)
        assert try_open(st, symbol="AAAUSDT", side="LONG", signal=_sig(sot=1), source_tags=["T"], filters=_filt())["ok"]
        r2 = try_open(st, symbol="AAAUSDT", side="LONG", signal=_sig(sot=2), source_tags=["T"], filters=_filt())
        assert r2["block_reason"] == "SAME_SYMBOL_POSITION"
        r3 = try_open(st, symbol="AAAUSDT", side="SHORT", signal=_sig(sot=3), source_tags=["T"], filters=_filt())
        assert r3["block_reason"] == "SAME_SYMBOL_POSITION"
    finally:
        _restore_default(saved)


def test_bulk_margin_and_risk_still_reject(tmp_path):
    saved, _, _ = _apply_bulk_isolated(tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=500.0, initial_capital_u=50_000, day_key=today_key(), day_start_equity_u=50_000)
        r = try_open(st, symbol="MUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())
        assert r["block_reason"] == "INSUFFICIENT_MARGIN"
        st2 = AccountState(run_id="t", cash_u=50_000, initial_capital_u=50_000, day_key=today_key(), day_start_equity_u=50_000)
        # ATR 過大 → 風險超標（不得縮止損通過）
        r2 = try_open(st2, symbol="RUSDT", side="LONG", signal=_sig(atr=5.0), source_tags=["T"], filters=_filt())
        assert r2["ok"] is False and r2["block_reason"] == "RISK_BUDGET_EXCEEDED"
    finally:
        _restore_default(saved)


def test_bulk_daily_loss_boundaries(tmp_path):
    saved, _, _ = _apply_bulk_isolated(tmp_path)
    try:
        assert config.DAILY_LOSS_LOCK_USDT == 2000.0
        # 低於 2000
        st = AccountState(
            run_id="t",
            cash_u=48_500,
            initial_capital_u=50_000,
            realized_net_u=-1_500,
            day_key=today_key(),
            day_start_equity_u=50_000,
            day_new_entries_locked=False,
        )
        assert st.equity_mark(0.0) > st.day_start_equity_u - 2000
        # 等於 2000
        st2 = AccountState(
            run_id="t",
            cash_u=48_000,
            initial_capital_u=50_000,
            realized_net_u=-2_000,
            day_key=today_key(),
            day_start_equity_u=50_000,
        )
        assert st2.equity_mark(0.0) <= st2.day_start_equity_u - 2000
        st2.day_new_entries_locked = True
        r = try_open(st2, symbol="XUSDT", side="LONG", signal=_sig(), source_tags=["T"], filters=_filt())
        assert r["block_reason"] == "DAILY_LOSS_LOCK"
        # 超過 2000 且有倉可退出
        assert try_open(st, symbol="YUSDT", side="LONG", signal=_sig(sot=9), source_tags=["T"], filters=_filt())["ok"]
        st.realized_net_u = -2_500
        st.day_new_entries_locked = True
        pos = get_position(st, "YUSDT")
        assert close_qty(st, "YUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="EXIT", book_ts=time.time())["ok"]
    finally:
        _restore_default(saved)


def test_bulk_lock_persists_restart_and_taipei_rollover(tmp_path):
    saved, _, _ = _apply_bulk_isolated(tmp_path)
    try:
        st = AccountState(
            run_id="t",
            cash_u=48_000,
            initial_capital_u=50_000,
            realized_net_u=-2_000,
            day_key=today_key(),
            day_start_equity_u=50_000,
            day_new_entries_locked=True,
        )
        save_account(st)
        st2 = load_or_create_account()
        assert st2.day_new_entries_locked is True
        assert abs(st2.day_start_equity_u - 50_000) < 1e-9
        # 跨日
        st2.day_key = "2000-01-01"
        ensure_day_rollover(st2, st2.equity_mark(0.0))
        assert st2.day_key == today_key()
        assert st2.day_new_entries_locked is False
        assert abs(st2.day_start_equity_u - st2.equity_mark(0.0)) < 1e-9
    finally:
        _restore_default(saved)


def test_bulk_writes_isolated_from_legacy_account(tmp_path):
    saved, _, root = _apply_bulk_isolated(tmp_path)
    try:
        legacy = Path("/Users/alan/MyProjects/automax/output/breakout_follow_v1/account_state.json")
        before = legacy.read_bytes() if legacy.exists() else None
        st = load_or_create_account()
        save_account(st)
        assert config.ACCOUNT_PATH.exists()
        assert config.PROFILE_ID == "paper_bulk_test_v1"
        assert config.ACCOUNT_PATH.parent.name == "paper_bulk_test_v1"
        assert config.KLINE_VAULT_DIR.parent.name == "paper_bulk_test_v1"
        # 不得寫入正式舊帳戶路徑
        assert Path(config.ACCOUNT_PATH).resolve() != Path(
            "/Users/alan/MyProjects/automax/output/breakout_follow_v1/account_state.json"
        ).resolve()
        if before is not None:
            assert legacy.read_bytes() == before
    finally:
        _restore_default(saved)


def test_bulk_same_profile_conflict_detection():
    # 解析
    assert _profile_from_cmdline("python -m breakout_follow_v1.paper_main --paper --hours 0") == "breakout_follow_v1"
    assert (
        _profile_from_cmdline("python -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1")
        == "paper_bulk_test_v1"
    )
    # 與預設 profile 衝突偵測：正式進程無 --profile → 視為 breakout_follow_v1
    # bulk profile 不應被其擋下（函式只回傳同 profile）
    hits_bulk = _find_conflicting_processes("paper_bulk_test_v1")
    # 可能空或僅 PROCESS_CHECK；若有 hit 必須含 paper_bulk_test_v1
    for h in hits_bulk:
        if h.startswith("PROCESS_CHECK"):
            continue
        assert "paper_bulk_test_v1" in h or "paper_main" in h


def test_no_live_order_path_reachable():
    try:
        assert_public_url("https://fapi.binance.com/fapi/v1/order")
        assert False, "should block"
    except LiveTradingForbidden:
        pass
    try:
        assert_public_url("https://fapi.binance.com/fapi/v1/klines")
    except LiveTradingForbidden:
        assert False, "public klines must be allowed"


def test_review_reconciles_and_handles_empty(tmp_path):
    saved, _, _ = _apply_bulk_isolated(tmp_path)
    try:
        st = load_or_create_account()
        path = write_review(st, {"coverage": {}, "duration_sec": 0, "candidates": 0, "opened": 0}, {"config_hash": "x"})
        text = path.read_text(encoding="utf-8")
        assert "勝率=n/a" in text or "樣本不足" in text
        assert "paper_bulk_test_v1" in text
        assert str(st.initial_capital_u) in text or "50000" in text
    finally:
        _restore_default(saved)


def test_decision_fields_do_not_change_open_outcome(tmp_path):
    saved, _, _ = _apply_bulk_isolated(tmp_path)
    try:
        st = AccountState(run_id="t", cash_u=50_000, initial_capital_u=50_000, day_key=today_key(), day_start_equity_u=50_000)
        r1 = try_open(st, symbol="OUSDT", side="LONG", signal=_sig(sot=1), source_tags=["T"], filters=_filt())
        assert r1["ok"] and r1.get("profile_id") == "paper_bulk_test_v1"
        # 同 key 去重結果不變
        pos = get_position(st, "OUSDT")
        close_qty(st, "OUSDT", qty=pos.qty_remaining, exit_price=pos.entry_price, reason="T", book_ts=time.time())
        r2 = try_open(st, symbol="OUSDT", side="LONG", signal=_sig(sot=1), source_tags=["T"], filters=_filt(), bypass_cooldown=True)
        assert r2["ok"] is False and r2["block_reason"] == "DUPLICATE_SIGNAL"
    finally:
        _restore_default(saved)


def test_claimed_reuse_params_match_overlay():
    from breakout_follow_v1.profiles import LEGACY_PROFILE_BASELINE, PAPER_BULK_TEST_V1

    assert PAPER_BULK_TEST_V1["MARGIN_PER_TRADE_USDT"] == 1000.0
    assert PAPER_BULK_TEST_V1["LEVERAGE"] == 10.0
    assert PAPER_BULK_TEST_V1["MAX_LOSS_PER_TRADE_USDT"] == 100.0
    assert PAPER_BULK_TEST_V1["MAX_PER_SYMBOL"] == 1
    assert PAPER_BULK_TEST_V1["COOLDOWN_MINUTES"] == 3
    assert PAPER_BULK_TEST_V1["TOP_N"] == 10
    assert LEGACY_PROFILE_BASELINE["DAILY_LOSS_LOCK_USDT"] == 300.0
    assert PAPER_BULK_TEST_V1["DAILY_LOSS_LOCK_USDT"] == 2000.0


def test_profile_flock_is_exclusive(tmp_path):
    """同 profile 原子 flock：第二把鎖必須失敗（非僅查 PID）。"""
    import fcntl
    import json
    from breakout_follow_v1.paper_main import acquire_profile_lock

    config.set_runtime_root(tmp_path)
    config.apply_profile("paper_bulk_test_v1")
    Path(config.ROOT_OUT).mkdir(parents=True, exist_ok=True)
    ok1, msg1 = acquire_profile_lock("paper_bulk_test_v1")
    assert ok1, msg1
    # 第二個 fd 對同路徑取非阻塞排他鎖 → 應失敗
    fh2 = open(config.LOCK_PATH, "a+", encoding="utf-8")
    try:
        blocked = False
        try:
            fcntl.flock(fh2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            blocked = True
        assert blocked is True
        meta = json.loads(Path(config.LOCK_PATH).read_text(encoding="utf-8"))
        assert meta["profile"] == "paper_bulk_test_v1"
    finally:
        fh2.close()
        config.apply_profile("breakout_follow_v1")
