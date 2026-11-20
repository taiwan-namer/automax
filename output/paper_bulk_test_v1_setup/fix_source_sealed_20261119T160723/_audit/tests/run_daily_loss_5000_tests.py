#!/usr/bin/env python3
"""日損門檻 5000 離線驗收（隔離 tmp 帳戶；不寫入運行中帳戶）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, reevaluate_daily_loss_lock, save_account
from breakout_follow_v1.portfolio import close_qty, try_open
from breakout_follow_v1.profiles import LEGACY_PROFILE_BASELINE, PAPER_BULK_TEST_V1
from breakout_follow_v1.util import now_iso, today_key

PASS = FAIL = 0
RESULTS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append(f"PASS {name}")
        print(f"PASS {name}")
    else:
        FAIL += 1
        RESULTS.append(f"FAIL {name} {detail}")
        print(f"FAIL {name} {detail}")


def _sig(atr=0.4, entry=100.0, sot=1):
    return {
        "est_fill_price": entry,
        "atr": atr,
        "atr_time": now_iso(),
        "signal_open_time": sot,
        "range_high": entry - 0.1,
        "range_low": entry - 1.0,
        "vol_x": 5.0,
        "spread": 0.01,
    }


def _filt():
    return {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5}


def main() -> int:
    check("profile_legacy_300", LEGACY_PROFILE_BASELINE["DAILY_LOSS_LOCK_USDT"] == 300.0)
    check("profile_bulk_5000", PAPER_BULK_TEST_V1["DAILY_LOSS_LOCK_USDT"] == 5000.0)

    td = Path(tempfile.mkdtemp(prefix="dl5000_"))
    config.apply_profile("paper_bulk_test_v1")
    config.ACCOUNT_PATH = td / "account_state.json"
    config.ROOT_OUT = td
    config.LEDGER_PATH = td / "ledger.jsonl"
    config.FILLS_PATH = td / "fills.jsonl"
    config.DECISION_TRACE_PATH = td / "decisions.jsonl"
    (td / "decision_snapshots").mkdir(parents=True, exist_ok=True)

    check("applied_5000", config.DAILY_LOSS_LOCK_USDT == 5000.0)

    # 低於 5000：不因日損門檻鎖；可否開倉由其他風控決定
    st = AccountState(
        run_id="t",
        cash_u=46_000.0,
        initial_capital_u=50_000,
        realized_net_u=-4_000.0,
        day_key=today_key(),
        day_start_equity_u=50_000.0,
        day_new_entries_locked=False,
    )
    eq = st.equity_mark(0.0)
    check("below_5000_not_breached", eq > st.day_start_equity_u - 5000.0)
    if eq <= st.day_start_equity_u - config.DAILY_LOSS_LOCK_USDT:
        st.day_new_entries_locked = True
    check("below_5000_unlocked", st.day_new_entries_locked is False)
    check("below_5000_would_breach_old_2000", eq <= st.day_start_equity_u - 2000.0)

    # 達到／超過 5000：阻止新倉；退出仍可
    st2 = AccountState(
        run_id="t2",
        cash_u=50_000,
        initial_capital_u=50_000,
        day_key=today_key(),
        day_start_equity_u=50_000.0,
    )
    opened = try_open(st2, symbol="AUSDT", side="LONG", signal=_sig(sot=1), source_tags=["T"], filters=_filt())
    check("seed_open_ok", opened.get("ok") is True)
    st2.realized_net_u = -5_000.0
    st2.cash_u = 50_000 + st2.realized_net_u - st2.used_margin()
    eq2 = st2.equity_mark(0.0)
    check("at_5000_breached", eq2 <= st2.day_start_equity_u - 5000.0)
    st2.day_new_entries_locked = True
    save_account(st2)
    blocked = try_open(st2, symbol="BUSDT", side="LONG", signal=_sig(sot=2), source_tags=["T"], filters=_filt())
    check("at_5000_blocks_new", blocked.get("ok") is False and blocked.get("block_reason") == "DAILY_LOSS_LOCK")
    pos = st2.positions.get("AUSDT")
    mark = 99.0
    closed = close_qty(st2, "AUSDT", qty=pos["qty_remaining"], exit_price=mark, reason="STOP", book_ts=1.0)
    check("at_5000_exit_ok", closed.get("ok") is True)

    # 重啟不抹除已發生損失；用原 day_start 重評
    st3 = AccountState(
        run_id="t3",
        cash_u=45_500.0,
        initial_capital_u=50_000,
        realized_net_u=-4_500.0,
        day_key=today_key(),
        day_start_equity_u=50_000.0,
        day_new_entries_locked=True,  # 模擬舊 2000 門檻 sticky 鎖
    )
    rep = reevaluate_daily_loss_lock(st3, floating_u=0.0, reason="test_raise_to_5000", apply=True)
    check("reeval_preserves_day_start", rep["day_start_equity_u"] == 50_000.0)
    check("reeval_limit_5000", rep["daily_loss_lock_usdt"] == 5000.0)
    check("reeval_loss_4500", abs(rep["day_loss_u"] - 4500.0) < 1e-6)
    check("reeval_unlock_when_under_5000", rep["should_lock"] is False and st3.day_new_entries_locked is False)

    st4 = AccountState(
        run_id="t4",
        cash_u=44_000.0,
        initial_capital_u=50_000,
        realized_net_u=-6_000.0,
        day_key=today_key(),
        day_start_equity_u=50_000.0,
        day_new_entries_locked=True,
    )
    rep4 = reevaluate_daily_loss_lock(st4, floating_u=0.0, reason="test_still_over_5000", apply=True)
    check("reeval_keep_lock_over_5000", rep4["should_lock"] is True and st4.day_new_entries_locked is True)

    print(f"\npassed={PASS} failed={FAIL}")
    out = ROOT / "DAILY_LOSS_5000_TEST_RESULTS.md"
    out.write_text(
        "# 日損 5000 離線測試\n\n"
        f"- passed: {PASS}\n- failed: {FAIL}\n\n"
        + "\n".join(f"- {r}" for r in RESULTS)
        + "\n",
        encoding="utf-8",
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
