"""Mirror entries from a source paper profile (same or opposite side). Paper-only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import config
from .account import save_account
from .market_data import fetch_book_tickers
from .portfolio import try_open
from .util import append_jsonl, now_iso
from . import console_zh


def _source_account_dir() -> Path:
    src = str(getattr(config, "MIRROR_SOURCE_PROFILE", "") or "").strip()
    if not src:
        raise ValueError("MIRROR_SOURCE_PROFILE empty")
    return (Path(config.RUNTIME_ROOT) / "output" / src).resolve()


def _mirror_state_path() -> Path:
    return Path(config.ROOT_OUT) / "mirror_state.json"


def load_mirror_state() -> dict[str, Any]:
    p = _mirror_state_path()
    if not p.exists():
        # 僅鏡像 C 啟動之後的來源新開倉；啟動前既有倉不回補
        return {
            "mirrored_source_trade_ids": [],
            "started_at": now_iso(),
            "ignore_source_opens_before": now_iso(),
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "mirrored_source_trade_ids": [],
            "started_at": now_iso(),
            "ignore_source_opens_before": now_iso(),
        }


def save_mirror_state(st: dict[str, Any]) -> None:
    Path(config.ROOT_OUT).mkdir(parents=True, exist_ok=True)
    _mirror_state_path().write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _iter_source_opens() -> list[dict[str, Any]]:
    root = _source_account_dir()
    paths = [root / "fills.jsonl"]
    runs = root / "runs"
    if runs.is_dir():
        for rd in sorted(runs.iterdir()):
            fp = rd / "fills.jsonl"
            if fp.exists():
                paths.append(fp)
    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "OPEN":
                continue
            tid = o.get("trade_id")
            if not tid:
                continue
            k = (tid, o.get("recorded_at"), round(float(o.get("fee_u") or 0), 8))
            if k in seen:
                continue
            seen.add(k)
            rows.append(o)
    rows.sort(key=lambda x: x.get("recorded_at") or "")
    return rows


def _est_fill_for_side(side: str, book: dict[str, Any]) -> Optional[float]:
    try:
        bid = float(book["bid"])
        ask = float(book["ask"])
    except Exception:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    slip = float(getattr(config, "SLIPPAGE_ADVERSE", 0.0002) or 0.0)
    if side == "LONG":
        return ask * (1 + slip)
    return bid * (1 - slip)


def _source_filters_for(sym: str) -> dict[str, Any]:
    """優先用來源帳戶 filters；缺則保守預設。"""
    try:
        p = _source_account_dir() / "account_state.json"
        if p.exists():
            obj = json.loads(p.read_text(encoding="utf-8"))
            f = (obj.get("filters") or {}).get(sym)
            if isinstance(f, dict) and f:
                return f
    except Exception:
        pass
    return {"stepSize": 0.001, "minQty": 0.0, "minNotional": 5.0, "tickSize": 0.0}


async def process_mirror_entries(
    gs,
    st,
    *,
    books: Optional[dict[str, Any]] = None,
    stats: Optional[dict[str, Any]] = None,
    store=None,
) -> dict[str, Any]:
    """讀取來源帳戶新 OPEN，於本帳戶開反向倉（固定毛退出由 try_open/profile 決定）。"""
    out = {"seen": 0, "opened": 0, "skipped": 0, "errors": 0}
    if not bool(getattr(config, "MIRROR_ONLY_ENTRIES", False)):
        return out
    src_profile = str(getattr(config, "MIRROR_SOURCE_PROFILE", "") or "").strip()
    if not src_profile:
        return out

    mstate = load_mirror_state()
    done = set(mstate.get("mirrored_source_trade_ids") or [])
    opens = _iter_source_opens()
    out["seen"] = len(opens)

    if books is None:
        books = await fetch_book_tickers(gs)

    ignore_before = str(mstate.get("ignore_source_opens_before") or "")
    for src in opens:
        tid = str(src.get("trade_id"))
        if tid in done:
            continue
        src_ts = str(src.get("recorded_at") or "")
        if ignore_before and src_ts and src_ts < ignore_before:
            done.add(tid)  # 標記略過，避免每輪重讀
            out["skipped"] += 1
            continue
        sym = str(src.get("symbol") or "")
        src_side = str(src.get("side") or "")
        if not sym or src_side not in ("LONG", "SHORT"):
            done.add(tid)
            out["skipped"] += 1
            continue
        inverse = bool(getattr(config, "MIRROR_INVERSE", True))
        if inverse:
            side = "SHORT" if src_side == "LONG" else "LONG"
        else:
            side = src_side
        book = (books or {}).get(sym)
        if not book:
            # 缺 book：本輪不標記 done，下輪再試
            out["errors"] += 1
            append_jsonl(
                config.DECISION_TRACE_PATH,
                {
                    "event": "MIRROR_SKIP",
                    "ts": now_iso(),
                    "reason": "NO_BOOK",
                    "source_trade_id": tid,
                    "symbol": sym,
                    "intended_side": side,
                    "profile_id": config.PROFILE_ID,
                    "mirror_source_profile": src_profile,
                    "mirror_inverse": inverse,
                },
            )
            continue

        est = _est_fill_for_side(side, book)
        if est is None or est <= 0:
            out["errors"] += 1
            continue

        atr = float(src.get("atr") or 0.0) or 1e-8
        tag = "MIRROR_INVERSE" if inverse else "MIRROR_SAME"
        sig = {
            "signal_open_time": int(src.get("signal_open_time") or 0),
            "est_fill_price": float(est),
            "atr": atr,
            "atr_time": str(src.get("recorded_at") or ""),
            "range_high": float(est) * 1.001,
            "range_low": float(est) * 0.999,
            "vol_x": 0.0,
            "direction_flip": inverse,
            "orig_side": src_side,
            "mirrored_from_trade_id": tid,
            "mirror_source_profile": src_profile,
            "mirror_inverse": inverse,
        }
        filt = (st.filters or {}).get(sym) or _source_filters_for(sym)
        # 鏡像進場：略過 cooldown／單筆風險上限（來源已開倉）；仍受 MAX_POS／同標／日損／保證金約束
        opened = try_open(
            st,
            symbol=sym,
            side=side,
            signal=sig,
            source_tags=[tag, src_profile],
            filters=filt,
            bypass_cooldown=True,
            bypass_risk_budget=True,
        )
        decision = {
            "event": "MIRROR_EVAL",
            "ts": now_iso(),
            "run_id": st.run_id,
            "profile_id": config.PROFILE_ID,
            "symbol": sym,
            "side": side,
            "source_trade_id": tid,
            "source_side": src_side,
            "source_profile": src_profile,
            "est_fill": est,
            "final_action": "OPEN" if opened.get("ok") else "BLOCKED",
            "first_reject_code": None if opened.get("ok") else opened.get("block_reason"),
            "block_reasons": opened.get("block_reasons") or ([opened.get("block_reason")] if opened.get("block_reason") else []),
            "trade_id": opened.get("trade_id"),
            "mirror_inverse": inverse,
        }
        if store is not None:
            store.log_decision(decision)
        else:
            append_jsonl(config.DECISION_TRACE_PATH, decision)

        if opened.get("ok"):
            out["opened"] += 1
            if stats is not None:
                stats["opened"] = int(stats.get("opened") or 0) + 1
            console_zh.print_open_event(sym, side, opened.get("trade_id"))
            mode = "反向" if inverse else "同向"
            print(
                f"[鏡像{mode}] 來源 {src_profile} {sym} {src_side} → 本帳戶 {side}｜src={tid}｜new={opened.get('trade_id')}",
                flush=True,
            )
            done.add(tid)
        else:
            # 永久性拒絕標記 done，避免每輪重試洗版；暫時性（NO book 已 continue）
            br = opened.get("block_reason")
            if br in (
                "SAME_SYMBOL_POSITION",
                "MAX_POSITIONS",
                "DAILY_LOSS_LOCK",
                "INSUFFICIENT_MARGIN",
                "OBSERVATION_CUTOFF",
                "RISK_BUDGET_EXCEEDED",
                "LOT_FILTER",
                "BARRIER_SIDE_INVALID",
                "INVALID_FIXED_GROSS_TARGETS",
                "STATE_INCONSISTENT",
            ):
                done.add(tid)
                out["skipped"] += 1
            else:
                out["errors"] += 1
            if stats is not None and br:
                rej = stats.setdefault("rejected", {})
                rej[br] = int(rej.get(br) or 0) + 1

    mstate["mirrored_source_trade_ids"] = sorted(done)
    mstate["updated_at"] = now_iso()
    mstate["source_profile"] = src_profile
    save_mirror_state(mstate)
    save_account(st)
    return out
