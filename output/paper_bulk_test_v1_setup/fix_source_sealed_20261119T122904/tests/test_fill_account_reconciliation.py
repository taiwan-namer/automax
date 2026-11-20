"""完整成交↔帳戶對帳：開倉／部分止盈／剩餘平倉／費用／未實現／權益；資金費明示缺漏。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from breakout_follow_v1 import config
from breakout_follow_v1.account import AccountState, get_position
from breakout_follow_v1.portfolio import (
    close_qty,
    estimate_net_unrealized,
    manage_position,
    mark_price_for_side,
    try_open,
)
from breakout_follow_v1.util import now_iso, today_key


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


def _book(mid: float, ts=None):
    half = mid * 0.0002
    return {
        "bid": mid - half,
        "ask": mid + half,
        "mid": mid,
        "spread": (2 * half) / mid,
        "fetched_at": ts or time.time(),
    }


def _diff(name: str, expected: float, actual: float, tol: float = 1e-6) -> dict:
    d = actual - expected
    return {
        "item": name,
        "expected": expected,
        "actual": actual,
        "diff": d,
        "ok": abs(d) <= tol,
    }


def _apply_isolated_bulk(tmp: Path):
    """獨立測試目錄；不得寫入正式 paper 輸出。"""
    config.set_runtime_root(tmp)
    applied = config.apply_profile("paper_bulk_test_v1")
    Path(config.ROOT_OUT).mkdir(parents=True, exist_ok=True)
    Path(config.KLINE_VAULT_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.DECISION_SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
    (Path(config.ROOT_OUT) / "runs").mkdir(parents=True, exist_ok=True)
    return applied


def _restore_legacy():
    config.apply_profile("breakout_follow_v1")


def test_full_lifecycle_reconciliation_with_diff_table(tmp_path):
    """
    案例：開倉 → 部分止盈 → 剩餘平倉。
    逐項 expected / actual / diff；funding_data_gap 必須為 True（不得當成已完整計費）。
    """
    formal_legacy = Path("/Users/alan/MyProjects/automax/output/breakout_follow_v1/account_state.json")
    formal_bulk = Path("/Users/alan/MyProjects/automax/output/paper_bulk_test_v1/account_state.json")
    before_legacy = formal_legacy.read_bytes() if formal_legacy.exists() else None
    before_bulk = formal_bulk.read_bytes() if formal_bulk.exists() else None

    _apply_isolated_bulk(tmp_path)
    rows: list[dict] = []
    try:
        assert "paper_bulk_test_v1" in str(config.ACCOUNT_PATH)
        assert Path(config.ACCOUNT_PATH).resolve() != formal_legacy.resolve()
        if formal_bulk.exists():
            assert Path(config.ACCOUNT_PATH).resolve() != formal_bulk.resolve()

        capital = 50_000.0
        entry = 100.0
        qty = config.NOTIONAL_USDT / entry  # 100
        open_fee = qty * entry * config.FEE_RATE_ONE_SIDE  # 5.0
        margin = config.MARGIN_PER_TRADE_USDT  # 1000

        st = AccountState(
            run_id="recon_v1",
            profile_id="paper_bulk_test_v1",
            cash_u=capital,
            initial_capital_u=capital,
            day_key=today_key(),
            day_start_equity_u=capital,
            funding_data_gap=True,
        )

        # --- OPEN ---
        r_open = try_open(st, symbol="RECONUSDT", side="LONG", signal=_sig(entry=entry, sot=1), source_tags=["RECON"], filters=_filt())
        assert r_open["ok"], r_open
        pos = get_position(st, "RECONUSDT")
        assert pos is not None
        rows.append(_diff("open.qty", qty, pos.qty_remaining))
        rows.append(_diff("open.entry_price", entry, pos.entry_price))
        rows.append(_diff("open.fee_u", open_fee, pos.fees_paid_u))
        rows.append(_diff("open.cash", capital - margin - open_fee, st.cash_u))
        rows.append(_diff("open.realized_fees", open_fee, st.realized_fees_u))
        rows.append(_diff("open.realized_net", -open_fee, st.realized_net_u))
        rows.append(
            {
                "item": "open.funding_data_gap",
                "expected": True,
                "actual": bool(pos.funding_gap and st.funding_data_gap),
                "diff": None,
                "ok": bool(pos.funding_gap and st.funding_data_gap),
                "note": "資金費缺漏；不得當成已完整計費",
            }
        )

        # --- 未實現（持倉中、報價上漲但未達部分止盈）---
        mid_u = 100.8
        book_u = _book(mid_u)
        mark_u = mark_price_for_side("LONG", book_u)
        unrl_exp = estimate_net_unrealized(pos, mark_u)
        # 手動公式核對
        gross_u = (mark_u - entry) * qty
        exit_fee_u = mark_u * qty * config.FEE_RATE_ONE_SIDE
        unrl_manual = gross_u - exit_fee_u
        rows.append(_diff("unrealized.formula_vs_helper", unrl_manual, unrl_exp))
        equity_u = st.equity_mark(unrl_exp)
        rows.append(_diff("equity.with_unrealized", st.cash_u + margin + unrl_exp, equity_u))

        # --- PARTIAL_TP via manage_position（達門檻）---
        from breakout_follow_v1.market_data import round_step

        mid_tp = 101.5
        book_tp = _book(mid_tp)
        mark_tp = mark_price_for_side("LONG", book_tp)
        qty_tp = round_step(qty * config.PARTIAL_TP_FRACTION, 0.001)
        events = manage_position(st, "RECONUSDT", book=book_tp, atr_latest=0.4)
        assert any(e.get("action") == "PARTIAL_TP" and e.get("ok") for e in events), events
        pos = get_position(st, "RECONUSDT")
        assert pos is not None and pos.partial_tp_done
        rem_exp = qty - qty_tp
        rows.append(_diff("partial.qty_closed", qty_tp, float(pos.partial_tp_qty)))
        rows.append(_diff("partial.qty_remaining", rem_exp, pos.qty_remaining))
        # 部分止盈費用與淨額
        gross_tp = (mark_tp - entry) * qty_tp
        fee_tp = mark_tp * qty_tp * config.FEE_RATE_ONE_SIDE
        net_tp = gross_tp - fee_tp
        fills = [json.loads(l) for l in Path(config.FILLS_PATH).read_text().splitlines() if l.strip()]
        partial_fills = [f for f in fills if f.get("event") == "CLOSE_PARTIAL"]
        assert len(partial_fills) == 1
        pf = partial_fills[0]
        rows.append(_diff("partial.fill.fee", fee_tp, float(pf["fee_u"])))
        rows.append(_diff("partial.fill.net", net_tp, float(pf["net_u"])))
        rows.append(
            {
                "item": "partial.funding_data_gap",
                "expected": True,
                "actual": bool(pf.get("funding_data_gap")),
                "diff": None,
                "ok": pf.get("funding_data_gap") is True,
                "note": "資金費缺漏明示",
            }
        )
        # 部分後現金：開倉後 + gross_tp - fee_tp（保證金未按比例釋放）
        cash_after_partial_exp = (capital - margin - open_fee) + gross_tp - fee_tp
        rows.append(_diff("partial.cash", cash_after_partial_exp, st.cash_u))
        rows.append(_diff("partial.margin_still_full", margin, st.used_margin()))

        # --- 剩餘平倉 ---
        mid_x = 101.2
        book_x = _book(mid_x)
        mark_x = mark_price_for_side("LONG", book_x)
        rem = pos.qty_remaining
        r_close = close_qty(st, "RECONUSDT", qty=rem, exit_price=mark_x, reason="RECON_FLAT", book_ts=time.time())
        assert r_close["ok"]
        assert "RECONUSDT" not in st.positions
        gross_x = (mark_x - entry) * rem
        fee_x = mark_x * rem * config.FEE_RATE_ONE_SIDE
        net_x = gross_x - fee_x
        rows.append(_diff("final.close.gross", gross_x, float(r_close["gross"])))
        rows.append(_diff("final.close.fee", fee_x, float(r_close["fee"])))
        rows.append(_diff("final.close.net", net_x, float(r_close["net"])))

        fees_total_exp = open_fee + fee_tp + fee_x
        rows.append(_diff("final.realized_fees", fees_total_exp, st.realized_fees_u))
        # flat：cash = capital + realized_net；equity = cash
        rows.append(_diff("final.cash_eq_capital_plus_net", capital + st.realized_net_u, st.cash_u))
        rows.append(_diff("final.equity_eq_cash", st.cash_u, st.equity_mark(0.0)))
        rows.append(
            {
                "item": "final.funding_data_gap_account",
                "expected": True,
                "actual": bool(st.funding_data_gap),
                "diff": None,
                "ok": st.funding_data_gap is True,
                "note": "帳戶層資金費缺口旗標仍為 True",
            }
        )

        # fills 事件序
        fills2 = [json.loads(l) for l in Path(config.FILLS_PATH).read_text().splitlines() if l.strip()]
        events_seq = [f["event"] for f in fills2]
        rows.append(
            {
                "item": "fills.sequence",
                "expected": ["OPEN", "CLOSE_PARTIAL", "CLOSE"],
                "actual": events_seq,
                "diff": None,
                "ok": events_seq == ["OPEN", "CLOSE_PARTIAL", "CLOSE"],
            }
        )

        failed = [r for r in rows if not r["ok"]]
        # 寫入獨立對帳報告（測試目錄內）
        report_path = Path(config.ROOT_OUT) / "RECONCILIATION_DIFF.json"
        report_path.write_text(
            json.dumps(
                {
                    "profile": "paper_bulk_test_v1",
                    "funding_note": "funding_data_gap=true；資金費未完整計入，不得宣稱完整淨收益",
                    "rows": rows,
                    "all_ok": len(failed) == 0,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert not failed, f"reconciliation diffs failed: {failed}"

        # 正式輸出未被觸碰
        if before_legacy is not None:
            assert formal_legacy.read_bytes() == before_legacy
        if before_bulk is not None:
            assert formal_bulk.read_bytes() == before_bulk
        assert not formal_bulk.exists() or Path(config.ACCOUNT_PATH).resolve() != formal_bulk.resolve()
    finally:
        _restore_legacy()


def test_empty_sample_na_is_not_reconciliation_pass(tmp_path):
    """空樣本 n/a 只代表無成交統計，不能當成對帳通過。"""
    from breakout_follow_v1.review import write_review

    _apply_isolated_bulk(tmp_path)
    try:
        st = AccountState(
            run_id="empty",
            profile_id="paper_bulk_test_v1",
            cash_u=50_000,
            initial_capital_u=50_000,
            day_key=today_key(),
            day_start_equity_u=50_000,
            funding_data_gap=True,
        )
        path = write_review(st, {"coverage": {}, "duration_sec": 0, "candidates": 0, "opened": 0}, {"config_hash": "x"})
        text = path.read_text(encoding="utf-8")
        assert "勝率=n/a" in text or "樣本不足" in text
        assert "funding_data_gap" in text
        # 明確：此測試只驗證空報表語意，不宣稱 fill↔account 對帳通過
        assert "RECONCILIATION_DIFF.json" not in text
    finally:
        _restore_legacy()
