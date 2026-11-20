from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .constants import TAIPEI


@dataclass
class Bar:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    is_closed: bool


def vault_path(setup_root: Path, profile_id: str) -> Path:
    return (
        setup_root
        / "fixed_data_root"
        / "data"
        / profile_id
        / "kline_vault_v2_clean"
    )


@lru_cache(maxsize=256)
def _load_csv(path_str: str) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    df = df.copy()
    df["ot"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(str(TAIPEI))
    return df


def load_bars(
    setup_root: Path,
    profile_id: str,
    symbol: str,
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[Bar]:
    path = vault_path(setup_root, profile_id) / f"{symbol}_1m.csv"
    df = _load_csv(str(path))
    if df.empty:
        return []
    out: List[Bar] = []
    for row in df.itertuples(index=False):
        ot = row.ot.to_pydatetime()
        if start is not None and ot + pd.Timedelta(minutes=1) <= start:
            # bar fully before start
            continue
        if end is not None and ot >= end:
            break
        out.append(
            Bar(
                open_time=ot,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                is_closed=bool(row.is_closed) if hasattr(row, "is_closed") else True,
            )
        )
    return out


def mid_of(bar: Bar) -> float:
    return (bar.high + bar.low) / 2.0
