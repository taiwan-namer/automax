"""Atomic account / position state for breakout_follow_v1."""

from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config
from .util import atomic_write_json, config_hash_v1, now_iso, today_key


@dataclass
class Position:
    trade_id: str
    symbol: str
    side: str  # LONG / SHORT
    qty_initial: float
    qty_remaining: float
    entry_price: float
    entry_time: str
    entry_ts: float
    margin_u: float
    notional_initial: float
    leverage: float
    atr_at_entry: float
    atr_time: str
    initial_stop: float
    effective_stop: float
    breakeven_stop: Optional[float] = None
    trailing_stop: Optional[float] = None
    partial_tp_done: bool = False
    partial_tp_qty: float = 0.0
    partial_tp_price: Optional[float] = None
    extreme_price: float = 0.0  # max for LONG / min for SHORT since entry
    fees_paid_u: float = 0.0
    funding_paid_u: float = 0.0
    funding_gap: bool = True
    realized_net_u: float = 0.0
    source_tags: list[str] = field(default_factory=list)
    signal_open_time: int = 0
    linked_prev_trade_id: Optional[str] = None
    range_high: float = 0.0
    range_low: float = 0.0
    vol_x: float = 0.0
    stop_set_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class AccountState:
    strategy_version: str = config.STRATEGY_VERSION
    initial_capital_u: float = config.INITIAL_CAPITAL_USDT
    cash_u: float = config.INITIAL_CAPITAL_USDT  # free cash (margin released on close)
    realized_net_u: float = 0.0
    realized_fees_u: float = 0.0
    realized_funding_u: float = 0.0
    funding_data_gap: bool = True
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbol -> Position dict
    cooldowns_until: dict[str, float] = field(default_factory=dict)
    reverse_events: dict[str, list[float]] = field(default_factory=dict)  # symbol -> timestamps
    processed_signals: list[str] = field(default_factory=list)  # capped
    day_key: str = ""
    day_start_equity_u: float = config.INITIAL_CAPITAL_USDT
    day_new_entries_locked: bool = False
    last_universe_refresh_ts: float = 0.0
    universe: list[dict[str, Any]] = field(default_factory=list)
    top_gainers: list[str] = field(default_factory=list)
    top_losers: list[str] = field(default_factory=list)
    filters: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbol -> lot filters
    run_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    inconsistency: Optional[str] = None
    accounting_migration_version: str = ""
    accounting_migration_meta: Optional[dict[str, Any]] = None

    def equity_mark(self, floating_u: float = 0.0) -> float:
        return self.initial_capital_u + self.realized_net_u + floating_u

    def used_margin(self) -> float:
        return sum(float(p.get("margin_u") or 0) for p in self.positions.values())

    def free_cash(self) -> float:
        return float(self.cash_u)

    def wallet_balance(self) -> float:
        """initial + realized（費用與 gross 已反映於 realized_net_u）。"""
        return float(self.initial_capital_u) + float(self.realized_net_u)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AccountState":
        obj = cls()
        for k, v in d.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj


def new_trade_id(run_id: str, symbol: str) -> str:
    return f"{run_id}:{symbol}:{uuid.uuid4().hex[:10]}"


def load_or_create_account() -> AccountState:
    path = config.ACCOUNT_PATH
    if path.exists():
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        st = AccountState.from_dict(data)
        # 重啟必須載入原帳戶，不得重設本金
        if abs(float(st.initial_capital_u) - config.INITIAL_CAPITAL_USDT) > 1e-9:
            # 允許歷史本金保留；不覆寫
            pass
        st.updated_at = now_iso()
        return st

    run_id = now_iso().replace(":", "").replace("+", "_")[:15] + "_" + uuid.uuid4().hex[:8]
    st = AccountState(
        run_id=run_id,
        created_at=now_iso(),
        updated_at=now_iso(),
        day_key=today_key(),
        day_start_equity_u=config.INITIAL_CAPITAL_USDT,
        cash_u=config.INITIAL_CAPITAL_USDT,
        initial_capital_u=config.INITIAL_CAPITAL_USDT,
    )
    save_account(st)
    return st


def save_account(st: AccountState) -> None:
    st.updated_at = now_iso()
    st.strategy_version = config.STRATEGY_VERSION
    # cap processed signals
    if len(st.processed_signals) > 5000:
        st.processed_signals = st.processed_signals[-3000:]
    atomic_write_json(config.ACCOUNT_PATH, st.to_dict())


def ensure_day_rollover(st: AccountState, equity_now: float) -> None:
    key = today_key()
    if st.day_key != key:
        st.day_key = key
        st.day_start_equity_u = equity_now
        st.day_new_entries_locked = False


def write_run_manifest(st: AccountState) -> dict[str, Any]:
    man = {
        "run_id": st.run_id,
        "strategy_id": config.STRATEGY_ID,
        "strategy_version": config.STRATEGY_VERSION,
        "config_hash": config_hash_v1(),
        "config_hash_algo": config.CONFIG_HASH_ALGO,
        "effective_from": now_iso(),
        "timezone": config.TZ_NAME,
        "account_path": str(config.ACCOUNT_PATH),
        "initial_capital_usdt": st.initial_capital_u,
        "capital": {
            "margin_per_trade": config.MARGIN_PER_TRADE_USDT,
            "leverage": config.LEVERAGE,
            "notional": config.NOTIONAL_USDT,
            "max_positions": config.MAX_CONCURRENT_POSITIONS,
            "max_loss_per_trade": config.MAX_LOSS_PER_TRADE_USDT,
            "daily_loss_lock": config.DAILY_LOSS_LOCK_USDT,
        },
        "simulation_assumptions": config.SIMULATION_ASSUMPTIONS,
        "notes": [
            "Independent paper strategy; does not touch legacy FADE/MOMO ledger",
            "SIMULATION_ASSUMPTION fees/slippage — not optimized, not guaranteed profitable",
            "Funding rate settlement may be marked funding_data_gap=true",
        ],
    }
    atomic_write_json(config.RUN_MANIFEST_PATH, man)
    return man


def get_position(st: AccountState, symbol: str) -> Optional[Position]:
    d = st.positions.get(symbol)
    if not d:
        return None
    return Position.from_dict(d)


def set_position(st: AccountState, pos: Position) -> None:
    st.positions[pos.symbol] = pos.to_dict()


def clear_position(st: AccountState, symbol: str) -> None:
    st.positions.pop(symbol, None)


def deep_copy_state(st: AccountState) -> AccountState:
    return AccountState.from_dict(copy.deepcopy(st.to_dict()))


def migrate_cash_gross_v1(
    st: AccountState,
    *,
    source_hash: str,
    reason: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    最小帳務遷移：使 cash = initial + realized − used_margin。
    可重複呼叫；同版本只生效一次。不改 realized／fills／歷史。
    """
    ver = config.ACCOUNTING_MIGRATION_VERSION
    cash_before = float(st.cash_u)
    used = float(st.used_margin())
    cash_target = float(st.initial_capital_u) + float(st.realized_net_u) - used
    equity_flat = float(st.initial_capital_u) + float(st.realized_net_u)
    already = getattr(st, "accounting_migration_version", "") == ver
    meta: dict[str, Any] = {
        "version": ver,
        "source_hash": source_hash,
        "reason": reason,
        "dry_run": dry_run,
        "already_applied": already,
        "applied": False,
        "cash_before": cash_before,
        "cash_after": cash_before if already else cash_target,
        "realized_net_u": float(st.realized_net_u),
        "used_margin": used,
        "equity_if_flat": equity_flat,
        "positions": len(st.positions),
        "funding_data_gap": bool(st.funding_data_gap),
        "ts": now_iso(),
    }
    if already:
        meta["skip_reason"] = "already_applied"
        return meta
    if dry_run:
        meta["cash_after"] = cash_target
        return meta
    st.cash_u = cash_target
    st.accounting_migration_version = ver
    st.accounting_migration_meta = {k: v for k, v in meta.items() if k != "dry_run"}
    st.accounting_migration_meta["applied"] = True
    meta["applied"] = True
    meta["cash_after"] = cash_target
    return meta
