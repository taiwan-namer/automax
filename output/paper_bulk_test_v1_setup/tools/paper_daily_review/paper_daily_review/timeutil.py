from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")

# 報告／復盤「交易日」結算界（Asia/Taipei）
# 日 D 視窗：[D-1 20:15, D 20:15)；於 D 20:25 產製日 D 報告。
SETTLEMENT_HOUR = 20
SETTLEMENT_MINUTE = 15
REPORT_HOUR = 20
REPORT_MINUTE = 25


def tz_from_name(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def parse_iso(s, *, default_tz: ZoneInfo = TAIPEI) -> Optional[datetime]:
    """Parse ISO string or epoch seconds/ms (int/float/numeric str)."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:  # ms
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=default_tz)
    text = str(s).strip().replace("Z", "+00:00")
    # numeric string epoch
    try:
        if text.replace(".", "", 1).isdigit():
            return parse_iso(float(text), default_tz=default_tz)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(default_tz)


def day_window(
    d: date,
    tz: ZoneInfo = TAIPEI,
    *,
    settlement_hour: int = SETTLEMENT_HOUR,
    settlement_minute: int = SETTLEMENT_MINUTE,
) -> Tuple[datetime, datetime]:
    """交易日 D 的半開區間：[D-1 settlement, D settlement)。"""
    end = datetime.combine(d, time(settlement_hour, settlement_minute), tzinfo=tz)
    start = end - timedelta(days=1)
    return start, end


def last_settled_day(
    asof: datetime,
    *,
    settlement_hour: int = SETTLEMENT_HOUR,
    settlement_minute: int = SETTLEMENT_MINUTE,
) -> date:
    """截至 asof，最近一次已結算完畢的交易日標籤（該日結束於 asof 當日或之前的 settlement）。"""
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=TAIPEI)
    else:
        asof = asof.astimezone(TAIPEI)
    settle_today = datetime.combine(
        asof.date(), time(settlement_hour, settlement_minute), tzinfo=TAIPEI
    )
    if asof >= settle_today:
        return asof.date()
    return asof.date() - timedelta(days=1)


def in_window(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    return dt is not None and start <= dt < end
