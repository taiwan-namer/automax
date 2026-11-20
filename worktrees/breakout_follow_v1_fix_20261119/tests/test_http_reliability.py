"""公開行情 HTTP 429/418/timeout/冷卻 — 隔離模擬，不打真實交易所。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from unittest import mock

import aiohttp

from breakout_follow_v1 import config
from breakout_follow_v1.market_data import (
    EndpointBanned,
    GuardedSession,
    PublicHttpError,
    _set_shared_cooldown,
    compute_backoff_seconds,
    get_http_event_log,
    parse_retry_after_seconds,
    reset_http_reliability_state_for_tests,
)


class _FakeResp:
    def __init__(self, status: int, payload: Any = None, headers: Optional[dict] = None, text: str = ""):
        self.status = status
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "t": time.time()})
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(**item)


def _run(coro):
    return asyncio.run(coro)


def test_parse_retry_after_seconds_and_backoff():
    assert parse_retry_after_seconds({"Retry-After": "2.5"}) == 2.5
    assert parse_retry_after_seconds({"Retry-After": "abc"}) is None
    assert parse_retry_after_seconds(None) is None
    # with Retry-After
    b = compute_backoff_seconds(0, retry_after=3.0)
    assert b >= 3.0
    # exponential bounded
    b0 = compute_backoff_seconds(0, retry_after=None)
    b3 = compute_backoff_seconds(10, retry_after=None)
    assert b0 >= config.HTTP_BACKOFF_BASE_SEC
    assert b3 <= config.HTTP_BACKOFF_MAX_SEC * (1 + config.HTTP_BACKOFF_JITTER_FRAC) + 1e-6


def test_429_with_retry_after_then_success():
    reset_http_reliability_state_for_tests()
    sess = _FakeSession(
        [
            {"status": 429, "headers": {"Retry-After": "0.05"}, "text": "slow"},
            {"status": 200, "payload": {"hello": 1}},
        ]
    )
    gs = GuardedSession(sess)  # type: ignore[arg-type]
    with mock.patch.object(config, "HTTP_OP_BUDGET_SEC", 10.0), mock.patch.object(config, "HTTP_MAX_ATTEMPTS", 5):
        data = _run(gs.get_json("/fapi/v1/time"))
    assert data == {"hello": 1}
    assert len(sess.calls) == 2
    ev = get_http_event_log()
    assert any(e.get("result") == "HTTP_429_BACKOFF" for e in ev)
    assert any(e.get("result") == "HTTP_OK_AFTER_RETRY" for e in ev)


def test_429_without_retry_after_uses_exp_backoff():
    reset_http_reliability_state_for_tests()
    sess = _FakeSession(
        [
            {"status": 429, "headers": {}, "text": "slow"},
            {"status": 200, "payload": {"ok": True}},
        ]
    )
    gs = GuardedSession(sess)  # type: ignore[arg-type]
    with mock.patch.object(config, "HTTP_BACKOFF_BASE_SEC", 0.05), mock.patch.object(config, "HTTP_OP_BUDGET_SEC", 10.0):
        data = _run(gs.get_json("/fapi/v1/klines", params={"symbol": "BTCUSDT"}))
    assert data["ok"] is True
    assert len(sess.calls) == 2


def test_retry_exhausted_and_cooldown_not_early():
    reset_http_reliability_state_for_tests()
    # 長 Retry-After 超過預算 → 不得提前再請求
    sess = _FakeSession([{"status": 429, "headers": {"Retry-After": "30"}, "text": "slow"}])
    gs = GuardedSession(sess)  # type: ignore[arg-type]
    with mock.patch.object(config, "HTTP_OP_BUDGET_SEC", 1.0), mock.patch.object(config, "HTTP_MAX_ATTEMPTS", 5):
        try:
            _run(gs.get_json("/fapi/v1/ticker/24hr"))
            assert False, "expected PublicHttpError"
        except PublicHttpError as exc:
            assert exc.status == 429
    assert len(sess.calls) == 1  # 冷却未滿不得第二發
    # 冷卻仍在
    remain = _run(_set_shared_cooldown("/fapi/v1/ticker/24hr", 0))  # just read path via set max
    # verify via second call while cool exceeds budget
    sess2 = _FakeSession([{"status": 200, "payload": {}}])
    gs2 = GuardedSession(sess2)  # type: ignore[arg-type]
    # force long remaining cooldown
    _run(_set_shared_cooldown("/fapi/v1/time", 60))
    with mock.patch.object(config, "HTTP_OP_BUDGET_SEC", 0.2):
        try:
            _run(gs2.get_json("/fapi/v1/time"))
            assert False
        except PublicHttpError as exc:
            assert "cooldown" in str(exc).lower() or "budget" in str(exc).lower()
    assert len(sess2.calls) == 0  # 未提前請求


def test_timeout_retries_then_fail():
    reset_http_reliability_state_for_tests()
    sess = _FakeSession(
        [
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ]
    )
    gs = GuardedSession(sess)  # type: ignore[arg-type]
    with mock.patch.object(config, "HTTP_MAX_ATTEMPTS", 3), mock.patch.object(config, "HTTP_BACKOFF_BASE_SEC", 0.01), mock.patch.object(
        config, "HTTP_OP_BUDGET_SEC", 5.0
    ):
        try:
            _run(gs.get_json("/fapi/v1/exchangeInfo"))
            assert False
        except PublicHttpError:
            pass
    assert len(sess.calls) == 3
    assert any(e.get("result") == "HTTP_TIMEOUT" for e in get_http_event_log())


def test_multi_worker_share_cooldown():
    reset_http_reliability_state_for_tests()
    call_times: list[float] = []

    class CoordSession:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            call_times.append(time.time())
            return _FakeResp(200, payload={"n": self.n})

    shared = CoordSession()
    gs_a = GuardedSession(shared)  # type: ignore[arg-type]
    gs_b = GuardedSession(shared)  # type: ignore[arg-type]

    async def both():
        # 先由 worker A 建立共用冷卻；worker B 不得在冷卻結束前發請求
        await _set_shared_cooldown("/fapi/v1/klines", 0.2)
        with mock.patch.object(config, "HTTP_OP_BUDGET_SEC", 5.0):
            t0 = time.time()
            t1 = asyncio.create_task(gs_a.get_json("/fapi/v1/klines"))
            t2 = asyncio.create_task(gs_b.get_json("/fapi/v1/klines"))
            out = await asyncio.gather(t1, t2)
            assert min(call_times) - t0 >= 0.15  # 兩 worker 皆等冷卻
            return out

    r1, r2 = _run(both())
    assert r1["n"] and r2["n"]
    assert len(call_times) == 2


def test_418_does_not_dense_retry():
    reset_http_reliability_state_for_tests()
    sess = _FakeSession(
        [
            {"status": 418, "text": "banned"},
            {"status": 200, "payload": {"should": "not"}},
        ]
    )
    gs = GuardedSession(sess)  # type: ignore[arg-type]
    try:
        _run(gs.get_json("/fapi/v1/klines"))
        assert False
    except EndpointBanned as exc:
        assert exc.status == 418
    assert len(sess.calls) == 1
    # second call immediately fails without new request
    try:
        _run(gs.get_json("/fapi/v1/klines"))
        assert False
    except EndpointBanned:
        pass
    assert len(sess.calls) == 1
