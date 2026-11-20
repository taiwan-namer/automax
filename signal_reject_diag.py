"""
NO_SIGNAL 子條件旁路診斷（不改交易決策）

用法：
  begin_probe(...) → detect_* 內部透過 record_* 寫入 → take_probe() 取回
  診斷寫入失敗不得改變 detect 回傳；寫檔錯誤有限度提示（非 except: pass）。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np
import pandas as pd

import config

_TLS = threading.local()
_WRITE_ERR_LAST_TS = 0.0
_WRITE_ERR_COUNT = 0


def _out(name: str) -> Path:
    return Path("output") / name


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def begin_probe(
    *,
    school: str,
    symbol: str = "",
    scan_time: Optional[str] = None,
    ctx: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """開始一輪旁路診斷；回傳同一份 dict 供 detect 內部填寫。"""
    probe: dict[str, Any] = {
        "scan_time": scan_time or _now_iso(),
        "symbol": symbol,
        "school": school,
        "signal_bar_open_time": None,
        "signal_bar_open_time_iso": None,
        "input_last_bar_open_time": None,
        "input_last_bar_close_time": None,
        "input_last_bar_open_time_iso": None,
        "bars_before_closed_filter": None,
        "bars_after_closed_filter": None,
        "excluded_unclosed_open_times": [],
        "first_reject_code": None,
        "conditions": [],
        "vol_metric": None,
        "routing_vol_ratio_note": (
            "routing recent_volume_ratio = mean(last RECENT_VOLUME_BARS_1M) / "
            "mean(last BASELINE_VOLUME_BARS_1M); detect vol_x = spike_bar_vol / "
            "mean(prior VOLUME_MA_PERIOD bars excluding spike). DIFFERENT metrics."
        ),
        "ctx": dict(ctx or {}),
        "result": "PENDING",
        **(extra or {}),
    }
    _TLS.probe = probe
    return probe


def current_probe() -> Optional[dict[str, Any]]:
    return getattr(_TLS, "probe", None)


def take_probe() -> Optional[dict[str, Any]]:
    """取出並清除目前 probe。"""
    p = getattr(_TLS, "probe", None)
    _TLS.probe = None
    return p


def clear_probe() -> None:
    _TLS.probe = None


@contextmanager
def probe_session(
    *,
    school: str,
    symbol: str = "",
    scan_time: Optional[str] = None,
    ctx: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Iterator[dict[str, Any]]:
    p = begin_probe(school=school, symbol=symbol, scan_time=scan_time, ctx=ctx, extra=extra)
    try:
        yield p
    finally:
        # 呼叫端應 take_probe；此處僅防漏清
        if getattr(_TLS, "probe", None) is p:
            _TLS.probe = None


def _append_condition(
    probe: dict[str, Any],
    *,
    code: str,
    status: str,
    actual: Any = None,
    op: Optional[str] = None,
    threshold: Any = None,
    unit: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    probe["conditions"].append(
        {
            "code": code,
            "status": status,  # PASS | FAIL | NOT_EVALUATED
            "actual": actual,
            "op": op,
            "threshold": threshold,
            "unit": unit,
            "detail": detail or {},
        }
    )


def mark_not_evaluated(probe: Optional[dict[str, Any]], codes: list[str]) -> None:
    if not probe:
        return
    existing = {c["code"] for c in probe.get("conditions") or []}
    for code in codes:
        if code in existing:
            continue
        _append_condition(probe, code=code, status="NOT_EVALUATED")


def reject(
    probe: Optional[dict[str, Any]],
    code: str,
    *,
    actual: Any = None,
    op: Optional[str] = None,
    threshold: Any = None,
    unit: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    remaining: Optional[list[str]] = None,
) -> None:
    """記錄第一拒絕原因，並把後續條件標 NOT_EVALUATED。"""
    if not probe:
        return
    if probe.get("first_reject_code") is None:
        probe["first_reject_code"] = code
        probe["result"] = "NO_SIGNAL"
    _append_condition(
        probe,
        code=code,
        status="FAIL",
        actual=actual,
        op=op,
        threshold=threshold,
        unit=unit,
        detail=detail,
    )
    if remaining:
        mark_not_evaluated(probe, remaining)


def pass_cond(
    probe: Optional[dict[str, Any]],
    code: str,
    *,
    actual: Any = None,
    op: Optional[str] = None,
    threshold: Any = None,
    unit: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    if not probe:
        return
    _append_condition(
        probe,
        code=code,
        status="PASS",
        actual=actual,
        op=op,
        threshold=threshold,
        unit=unit,
        detail=detail,
    )


def mark_signal_pass(probe: Optional[dict[str, Any]], side: str, entry: float, signal_key: str) -> None:
    if not probe:
        return
    probe["first_reject_code"] = None
    probe["result"] = "SIGNAL"
    probe["signal_side"] = side
    probe["signal_entry"] = entry
    probe["signal_key"] = signal_key


def set_bar_meta(
    probe: Optional[dict[str, Any]],
    *,
    df_raw: pd.DataFrame,
    closed: pd.DataFrame,
    interval_ms: int = 60_000,
    signal_idx: Optional[int] = None,
) -> None:
    if not probe:
        return
    probe["bars_before_closed_filter"] = int(len(df_raw)) if df_raw is not None else 0
    probe["bars_after_closed_filter"] = int(len(closed)) if closed is not None else 0
    if df_raw is not None and not df_raw.empty and "open_time" in df_raw.columns:
        last_ot = int(df_raw["open_time"].iloc[-1])
        probe["input_last_bar_open_time"] = last_ot
        probe["input_last_bar_close_time"] = last_ot + interval_ms
        probe["input_last_bar_open_time_iso"] = _ms_to_iso(last_ot)
        # 被排除的未收盤根（通常最多最後一根）
        if closed is not None and not closed.empty:
            closed_ots = set(closed["open_time"].astype(np.int64).tolist())
            raw_ots = df_raw["open_time"].astype(np.int64).tolist()
            excl = [int(ot) for ot in raw_ots if int(ot) not in closed_ots]
            probe["excluded_unclosed_open_times"] = excl[-3:]
            probe["excluded_unclosed_open_times_iso"] = [_ms_to_iso(x) for x in excl[-3:]]
        elif df_raw is not None and not df_raw.empty:
            probe["excluded_unclosed_open_times"] = [last_ot]
            probe["excluded_unclosed_open_times_iso"] = [_ms_to_iso(last_ot)]
    if (
        signal_idx is not None
        and closed is not None
        and not closed.empty
        and 0 <= signal_idx < len(closed)
    ):
        sot = int(closed["open_time"].iloc[signal_idx])
        probe["signal_bar_open_time"] = sot
        probe["signal_bar_open_time_iso"] = _ms_to_iso(sot)


def build_vol_metric(
    *,
    spike_idx: int,
    spike_vol: float,
    vol_ma: float,
    vol_x: float,
    ma_period: int,
    closed: pd.DataFrame,
    exclude_spike: bool = True,
) -> dict[str, Any]:
    """量倍細節：分子=訊號／爆量根；分母=前 ma_period 根（預設排除訊號根）。"""
    start = spike_idx - ma_period
    end = spike_idx  # [start, end) excludes spike
    denom_ots: list[int] = []
    if start >= 0 and not closed.empty:
        denom_ots = [int(x) for x in closed["open_time"].iloc[start:end].tolist()]
    spike_ot = int(closed["open_time"].iloc[spike_idx]) if 0 <= spike_idx < len(closed) else None
    return {
        "numerator_bar": "spike_bar",
        "numerator_open_time": spike_ot,
        "numerator_open_time_iso": _ms_to_iso(spike_ot),
        "numerator_volume": float(spike_vol),
        "denominator_window": {
            "start_idx": start,
            "end_idx_exclusive": end,
            "bars": ma_period,
            "open_times": denom_ots[:3] + (["..."] if len(denom_ots) > 6 else denom_ots[3:-3]) + denom_ots[-3:]
            if len(denom_ots) > 6
            else denom_ots,
            "open_time_first": denom_ots[0] if denom_ots else None,
            "open_time_last": denom_ots[-1] if denom_ots else None,
            "mean_volume": float(vol_ma),
        },
        "exclude_signal_bar_from_denominator": bool(exclude_spike),
        "vol_x": float(vol_x) if vol_ma > 0 else None,
        "vol_ma_zero_or_insufficient": bool(vol_ma <= 0 or start < 0),
        "vs_routing_recent_volume_ratio": "DIFFERENT_METRIC",
    }


def warn_write_error(where: str, exc: BaseException) -> None:
    """有限度錯誤提示：節流，不吞掉、不影響交易。"""
    global _WRITE_ERR_LAST_TS, _WRITE_ERR_COUNT
    _WRITE_ERR_COUNT += 1
    now = time.time()
    if now - _WRITE_ERR_LAST_TS < 30.0 and _WRITE_ERR_COUNT > 1:
        return
    _WRITE_ERR_LAST_TS = now
    print(f"[diag-write-warn] {where}: {type(exc).__name__}: {exc} (count={_WRITE_ERR_COUNT})")


def append_reject_diag_jsonl(record: dict[str, Any], *, path: Optional[Path] = None) -> None:
    """寫入旁路診斷 JSONL（非正式 live decision_trace 替代品亦可）。"""
    target = path or _out(f"signal_reject_diag_{datetime.now().astimezone().strftime('%Y%m%d')}.jsonl")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        warn_write_error(str(target), exc)


# ---------------------------------------------------------------------------
# 條件代碼順序（與 detect 短路一致；後續未跑 = NOT_EVALUATED）
# ---------------------------------------------------------------------------

MOMO_COND_ORDER = [
    "CLOSED_FILTER",
    "INSUFFICIENT_CLOSED_BARS",
    "VOL_MA_ZERO",
    "VOL_X_RANGE",
    "ZOMBIE_LIQUIDITY",
    "BODY_RATIO",
    "DIRECTION_POS_GATE",
]

FADE_COND_ORDER = [
    "CLOSED_FILTER",
    "INSUFFICIENT_CLOSED_BARS",
    "VOL_MA_ZERO",
    "VOL_X_RANGE",
    "ZOMBIE_LIQUIDITY",
    "FADE_SHORT_PATTERN",
    "FADE_SHORT_DISABLED",
    "FADE_LONG_PATTERN",
]


def remaining_after(order: list[str], code: str) -> list[str]:
    try:
        i = order.index(code)
    except ValueError:
        return []
    return order[i + 1 :]
