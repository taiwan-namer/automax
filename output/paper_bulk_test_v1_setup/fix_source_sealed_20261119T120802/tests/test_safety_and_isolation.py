"""Safety: live order paths blocked; I/O stays inside tmp."""
from __future__ import annotations

import hashlib
from pathlib import Path

from breakout_follow_v1 import config
from breakout_follow_v1.safety import LiveTradingForbidden, assert_public_url, guard_session_request
from tests._isolation import isolate_all_paths, restore_paths

REPO = Path(__file__).resolve().parents[3]
LIVE_OUT = REPO / "output" / "breakout_follow_v1"


def test_private_order_paths_blocked():
    for url in (
        "https://fapi.binance.com/fapi/v1/order",
        "https://fapi.binance.com/fapi/v1/batchOrder",
        "https://fapi.binance.com/fapi/v1/listenKey",
    ):
        try:
            assert_public_url(url)
            raise AssertionError(f"should block {url}")
        except LiveTradingForbidden:
            pass
    try:
        guard_session_request("POST", "https://fapi.binance.com/fapi/v1/klines")
        raise AssertionError("POST should be blocked")
    except LiveTradingForbidden:
        pass
    # paper-named but private path still blocked
    try:
        assert_public_url("https://fapi.binance.com/fapi/v1/order?paper=1")
        raise AssertionError("paper query must not bypass")
    except LiveTradingForbidden:
        pass
    assert_public_url("https://fapi.binance.com/fapi/v1/klines")


def test_paths_redirected_not_live(tmp_path):
    saved = isolate_all_paths(config, tmp_path)
    try:
        assert Path(config.ROOT_OUT).resolve().is_relative_to(Path(tmp_path).resolve())
        assert Path(config.FILLS_PATH).resolve().is_relative_to(Path(tmp_path).resolve())
        assert Path(config.ACCOUNT_PATH).resolve().is_relative_to(Path(tmp_path).resolve())
        assert Path(config.KLINE_VAULT_DIR).resolve().is_relative_to(Path(tmp_path).resolve())
        assert "kline_vault_v2_clean" in str(config.KLINE_VAULT_DIR)
        # writing fills must not touch live
        from breakout_follow_v1.util import append_jsonl

        before = hashlib.sha256((LIVE_OUT / "fills.jsonl").read_bytes()).hexdigest()
        append_jsonl(config.FILLS_PATH, {"event": "TEST_ISOLATION", "note": "tmp only"})
        after = hashlib.sha256((LIVE_OUT / "fills.jsonl").read_bytes()).hexdigest()
        assert before == after
        assert config.FILLS_PATH.exists()
        assert "TEST_ISOLATION" in config.FILLS_PATH.read_text()
    finally:
        restore_paths(config, saved)
