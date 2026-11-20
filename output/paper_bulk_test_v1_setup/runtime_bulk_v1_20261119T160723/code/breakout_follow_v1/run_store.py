"""
Run-scoped archival for overnight paper (observability only — no trade rule changes).

Layout:
  output/breakout_follow_v1/runs/{run_id}/
    run_manifest.json
    account_state.json          # mirrored snapshot (live account still at ROOT)
    launch.json
    code_hashes.json
    decision_trace.jsonl
    fills.jsonl
    trades.csv
    equity_snapshots.jsonl
    universe_history.jsonl
    quote_events.jsonl          # prices actually used for manage/entry
    stop_events.jsonl
    position_events.jsonl
    data_quality.jsonl
    data_quality.md
    review.md
    klines_1m/{SYMBOL}_1m.csv   # append-only, dedupe by open_time
    decision_snapshots/         # immutable probe dumps
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from . import config
from .util import append_jsonl, atomic_write_json, config_hash_v1, now_iso, stable_config_payload

_LOCK = threading.RLock()
_CURRENT: Optional["RunStore"] = None

WARMUP_MIN_BARS = 100
PKG_DIR = Path(__file__).resolve().parent


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PKG_DIR.parent),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "UNKNOWN"


def new_run_id() -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


@dataclass
class RunStore:
    run_id: str
    root: Path
    created_at: str = field(default_factory=now_iso)
    quality_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def kline_dir(self) -> Path:
        return self.root / "klines_1m"

    def path(self, name: str) -> Path:
        return self.root / name

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.kline_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "decision_snapshots").mkdir(parents=True, exist_ok=True)

    def write_launch_meta(self, *, command: list[str], hours: float, once: bool) -> None:
        atomic_write_json(
            self.path("launch.json"),
            {
                "run_id": self.run_id,
                "created_at": self.created_at,
                "timezone": config.TZ_NAME,
                "command": command,
                "hours": hours,
                "once": once,
                "cwd": str(Path.cwd().resolve()),
                "pid": os.getpid(),
                "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
                "profile_id": getattr(config, "PROFILE_ID", config.STRATEGY_ID),
                "active_profile": getattr(config, "ACTIVE_PROFILE", None),
                "accounting_migration_version": getattr(config, "ACCOUNTING_MIGRATION_VERSION", None),
                "package_dir": str(PKG_DIR.resolve()),
                "account_path": str(config.ACCOUNT_PATH.resolve()),
                "kline_vault_dir": str(config.KLINE_VAULT_DIR.resolve()),
                "initial_capital_usdt": config.INITIAL_CAPITAL_USDT,
                "max_concurrent_positions": config.MAX_CONCURRENT_POSITIONS,
                "daily_loss_lock_usdt": config.DAILY_LOSS_LOCK_USDT,
                "runtime_root": str(Path(getattr(config, "RUNTIME_ROOT", ".")).resolve()),
                "root_out": str(Path(config.ROOT_OUT).resolve()),
                "reverse_policy": getattr(config, "ACTIVE_PROFILE", None) == "paper_bulk_test_v1"
                and "approved_full_breakout_reverse_reopen_20261119"
                or "legacy_engine_reverse",
                "http_reliability": {
                    "request_timeout_sec": getattr(config, "HTTP_REQUEST_TIMEOUT_SEC", None),
                    "max_attempts": getattr(config, "HTTP_MAX_ATTEMPTS", None),
                    "op_budget_sec": getattr(config, "HTTP_OP_BUDGET_SEC", None),
                    "note": "process-local cooldown only; no cross-process global rate limit",
                },
                "simulation_assumptions": config.SIMULATION_ASSUMPTIONS,
            },
        )

    def write_code_hashes(self) -> dict[str, Any]:
        files = sorted(PKG_DIR.glob("*.py"))
        hashes = {str(p.relative_to(PKG_DIR.parent)): _sha256_file(p) for p in files}
        payload = {
            "run_id": self.run_id,
            "git_commit": _git_commit(),
            "strategy_version": config.STRATEGY_VERSION,
            "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
            "config_hash": config_hash_v1(),
            "config_hash_algo": config.CONFIG_HASH_ALGO,
            "files": hashes,
            "recorded_at": now_iso(),
        }
        atomic_write_json(self.path("code_hashes.json"), payload)
        atomic_write_json(self.path("config_snapshot.json"), stable_config_payload())
        return payload

    def snapshot_code_tree(self) -> Path:
        """完整實際程式碼快照（不可變目錄；不依賴 hash 還原）。"""
        dest = self.path("code_snapshot") / "breakout_follow_v1"
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            PKG_DIR,
            dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        manifest = []
        for p in sorted(dest.rglob("*")):
            if p.is_file():
                manifest.append(
                    {
                        "rel": str(p.relative_to(dest.parent)),
                        "sha256": _sha256_file(p),
                        "bytes": p.stat().st_size,
                    }
                )
        atomic_write_json(
            self.path("code_snapshot") / "MANIFEST.json",
            {
                "run_id": self.run_id,
                "source_package_dir": str(PKG_DIR.resolve()),
                "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
                "recorded_at": now_iso(),
                "files": manifest,
            },
        )
        return dest

    def write_account_at_start_immutable(self, account_snapshot: dict[str, Any]) -> Path:
        """不可變起始帳戶快照；後續 mirror_account 不得覆寫此檔。"""
        path = self.path("account_at_start.json")
        if path.exists():
            return path
        payload = {
            "run_id": self.run_id,
            "recorded_at": now_iso(),
            "immutable": True,
            "account": account_snapshot,
        }
        atomic_write_json(path, payload)
        return path

    def write_run_manifest(self, account_snapshot: dict[str, Any], extra: Optional[dict] = None) -> dict[str, Any]:
        self.write_account_at_start_immutable(account_snapshot)
        man = {
            "run_id": self.run_id,
            "strategy_id": config.STRATEGY_ID,
            "strategy_version": config.STRATEGY_VERSION,
            "code_fix_tag": getattr(config, "CODE_FIX_TAG", None),
            "config_hash": config_hash_v1(),
            "config_hash_algo": config.CONFIG_HASH_ALGO,
            "git_commit": _git_commit(),
            "effective_from": self.created_at,
            "timezone": config.TZ_NAME,
            "paths": {
                "run_root": str(self.root),
                "account_live": str(config.ACCOUNT_PATH),
                "account_at_start": str(self.path("account_at_start.json")),
                "code_snapshot": str(self.path("code_snapshot")),
                "klines_archive": str(self.kline_dir),
                "rolling_vault": str(config.KLINE_VAULT_DIR),
            },
            "account_at_start": {
                "initial_capital_u": account_snapshot.get("initial_capital_u"),
                "cash_u": account_snapshot.get("cash_u"),
                "realized_net_u": account_snapshot.get("realized_net_u"),
                "realized_fees_u": account_snapshot.get("realized_fees_u"),
                "day_key": account_snapshot.get("day_key"),
                "day_new_entries_locked": account_snapshot.get("day_new_entries_locked"),
                "accounting_migration_version": account_snapshot.get("accounting_migration_version"),
                "positions": list((account_snapshot.get("positions") or {}).keys()),
                "full_snapshot": "account_at_start.json",
            },
            "simulation_assumptions": config.SIMULATION_ASSUMPTIONS,
            "notes": [
                "Observability run archive",
                "account_at_start.json is immutable; account_state.json may be mirrored later",
                "Klines in klines_1m/ are append-only for this run (not rolling-truncated)",
                "Do not overwrite older runs under runs/",
            ],
            **(extra or {}),
        }
        atomic_write_json(self.path("run_manifest.json"), man)
        # pointer for export tooling (does not delete old runs)
        atomic_write_json(config.ROOT_OUT / "current_run.json", {"run_id": self.run_id, "root": str(self.root), "updated_at": now_iso()})
        return man

    def mirror_account(self, account: dict[str, Any]) -> None:
        """持續更新的鏡像；不得碰 account_at_start.json。"""
        atomic_write_json(self.path("account_state.json"), account)

    def log_decision(self, row: dict[str, Any]) -> None:
        row = {"run_id": self.run_id, "recorded_at": now_iso(), **row}
        append_jsonl(self.path("decision_trace.jsonl"), row)
        # root mirror（相容舊路徑；同一事件只由呼叫端或此處其一寫入 root）
        append_jsonl(config.DECISION_TRACE_PATH, row)

    def log_fill(self, row: dict[str, Any]) -> None:
        """僅寫入本 run；root fills 由 portfolio._append_fill 負責，避免雙寫。"""
        row = {"run_id": self.run_id, **row}
        if "recorded_at" not in row:
            row["recorded_at"] = now_iso()
        append_jsonl(self.path("fills.jsonl"), row)

    def log_equity(self, row: dict[str, Any], *, reason: str = "MINUTE") -> None:
        row = {"run_id": self.run_id, "recorded_at": now_iso(), "snapshot_reason": reason, **row}
        append_jsonl(self.path("equity_snapshots.jsonl"), row)
        append_jsonl(config.EQUITY_PATH, row)

    def log_universe(self, snap: dict[str, Any]) -> None:
        row = {
            "run_id": self.run_id,
            "recorded_at": now_iso(),
            "data_time": snap.get("refreshed_at"),
            "eligible_count": snap.get("eligible_count"),
            "top_gainers": snap.get("top_gainers"),
            "top_losers": snap.get("top_losers"),
            "rank_basis": "price_change_pct_24h from /fapi/v1/ticker/24hr",
            "min_quote_volume_usdt": config.MIN_QUOTE_VOLUME_USDT,
            "eligible_symbols": [x.get("symbol") for x in (snap.get("eligible") or [])],
        }
        append_jsonl(self.path("universe_history.jsonl"), row)
        atomic_write_json(self.path("universe_latest.json"), row)
        atomic_write_json(config.UNIVERSE_PATH, {
            "refreshed_at": snap.get("refreshed_at"),
            "eligible_count": snap.get("eligible_count"),
            "top_gainers": snap.get("top_gainers"),
            "top_losers": snap.get("top_losers"),
        })

    def log_quote_event(self, row: dict[str, Any]) -> None:
        append_jsonl(self.path("quote_events.jsonl"), {"run_id": self.run_id, "recorded_at": now_iso(), **row})

    def log_manage_event(self, row: dict[str, Any]) -> None:
        """事件化管理完成紀錄（非定時心跳）。"""
        append_jsonl(self.path("manage_events.jsonl"), {"run_id": self.run_id, "recorded_at": now_iso(), **row})

    def log_stop_event(self, row: dict[str, Any]) -> None:
        append_jsonl(self.path("stop_events.jsonl"), {"run_id": self.run_id, "recorded_at": now_iso(), **row})

    def log_position_event(self, row: dict[str, Any]) -> None:
        append_jsonl(self.path("position_events.jsonl"), {"run_id": self.run_id, "recorded_at": now_iso(), **row})

    def save_decision_snapshot(self, key: str, payload: dict[str, Any]) -> Path:
        safe = key.replace(":", "_").replace("/", "_")
        path = self.root / "decision_snapshots" / f"{safe}.json"
        if path.exists():
            return path  # immutable once written
        atomic_write_json(path, payload)
        return path

    def archive_klines(
        self,
        symbol: str,
        df: pd.DataFrame,
        *,
        note: str = "update",
        fetched_at_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Merge into run archive latest-view; never shrink history; never keep-first stale bars."""
        from .market_data import annotate_bar_closed, merge_klines

        meta: dict[str, Any] = {"symbol": symbol, "note": note, "ok": False}
        if df is None or getattr(df, "empty", True):
            meta["error"] = "empty_df"
            self.note_quality("KLINE_EMPTY", symbol=symbol, detail=note)
            return meta
        path = self.kline_dir / f"{symbol}_1m.csv"
        keep = [
            c
            for c in (
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "interval",
                "is_closed",
                "fetched_at_ms",
            )
            if c in df.columns
        ]
        incoming = df[keep].copy()
        fat = int(fetched_at_ms or int(time.time() * 1000))
        if "is_closed" not in incoming.columns or "fetched_at_ms" not in incoming.columns:
            incoming = annotate_bar_closed(incoming, fat, interval="1m")
        with _LOCK:
            if path.exists():
                try:
                    old = pd.read_csv(path)
                except Exception as exc:
                    self.note_quality("KLINE_READ_FAIL", symbol=symbol, detail=str(exc))
                    old = pd.DataFrame()
                merged = merge_klines(old, incoming)
            else:
                merged = merge_klines(None, incoming)
            # is_closed 依抓取當下判定，禁止無條件 True
            merged.to_csv(path, index=False)
        meta.update(
            {
                "ok": True,
                "bars": len(merged),
                "path": str(path),
                "first_ot": int(merged["open_time"].iloc[0]),
                "last_ot": int(merged["open_time"].iloc[-1]),
                "closed_bars": int(merged["is_closed"].astype(bool).sum()) if "is_closed" in merged.columns else None,
            }
        )
        if len(merged) < WARMUP_MIN_BARS:
            self.note_quality(
                "KLINE_WARMUP_SHORT",
                symbol=symbol,
                detail=f"bars={len(merged)}<{WARMUP_MIN_BARS}",
                replay_precise=False,
            )
        return meta

    def seed_warmup_from_vault(self, symbols: list[str]) -> dict[str, Any]:
        """Copy existing vault bars into run archive (need ≥100 when available)."""
        summary = {"symbols": 0, "short": [], "missing": [], "ok": []}
        for sym in symbols:
            vault = config.KLINE_VAULT_DIR / f"{sym}_1m.csv"
            if not vault.exists():
                summary["missing"].append(sym)
                self.note_quality("WARMUP_VAULT_MISSING", symbol=sym, replay_precise=False)
                continue
            try:
                df = pd.read_csv(vault)
            except Exception as exc:
                summary["missing"].append(sym)
                self.note_quality("WARMUP_VAULT_READ_FAIL", symbol=sym, detail=str(exc), replay_precise=False)
                continue
            meta = self.archive_klines(sym, df, note="warmup_seed")
            summary["symbols"] += 1
            if meta.get("bars", 0) < WARMUP_MIN_BARS:
                summary["short"].append({"symbol": sym, "bars": meta.get("bars")})
            else:
                summary["ok"].append(sym)
        atomic_write_json(self.path("warmup_seed_report.json"), {"recorded_at": now_iso(), **summary})
        return summary

    def note_quality(self, code: str, *, symbol: str = "", detail: str = "", replay_precise: Optional[bool] = None) -> None:
        ev = {
            "ts": now_iso(),
            "code": code,
            "symbol": symbol,
            "detail": detail,
            "replay_precise": replay_precise,
        }
        self.quality_events.append(ev)
        append_jsonl(self.path("data_quality.jsonl"), {"run_id": self.run_id, **ev})

    def write_data_quality_md(self) -> Path:
        path = self.path("data_quality.md")
        # reload jsonl for completeness
        events = []
        jl = self.path("data_quality.jsonl")
        if jl.exists():
            for line in jl.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
        by_code: dict[str, int] = {}
        for e in events:
            by_code[e.get("code", "?")] = by_code.get(e.get("code", "?"), 0) + 1

        # current archive inventory (truth after backfill; does not erase history)
        short_now: list[str] = []
        ok_now = 0
        bar_min = None
        bar_max = None
        n_csv = 0
        if self.kline_dir.exists():
            for csvp in sorted(self.kline_dir.glob("*_1m.csv")):
                n_csv += 1
                try:
                    n = max(0, sum(1 for _ in csvp.open("r", encoding="utf-8")) - 1)
                except Exception:
                    n = 0
                    short_now.append(csvp.stem.replace("_1m", ""))
                    continue
                bar_min = n if bar_min is None else min(bar_min, n)
                bar_max = n if bar_max is None else max(bar_max, n)
                if n < WARMUP_MIN_BARS:
                    short_now.append(csvp.stem.replace("_1m", ""))
                else:
                    ok_now += 1

        lines = [
            f"# data_quality — run `{self.run_id}`",
            "",
            f"> 更新：{now_iso()}｜**未**虛構缺漏資料",
            "",
            "## 目前歸檔狀態（以磁碟為準）",
            "",
            f"- `klines_1m/` 檔案數：{n_csv}",
            f"- bars 範圍：min={bar_min} max={bar_max}｜達標（≥{WARMUP_MIN_BARS}）：{ok_now}",
            f"- 目前仍短於暖機門檻：{len(short_now)}"
            + (f" — {', '.join(short_now[:20])}" if short_now else "（無）"),
            f"- 1m K 精確重播（暖機）：{'可（全部達標）' if n_csv and not short_now else '不可（見上列或缺檔）'}",
            "- 逐筆 tick：**不可**精確重播（僅保存實際使用之 quote_events）",
            "",
            "## 歷史品質事件摘要（append-only，含已修復前紀錄）",
            "",
            f"- 事件數：{len(events)}",
            "",
            "| code | n |",
            "|---|---:|",
        ]
        for k, v in sorted(by_code.items(), key=lambda x: -x[1]):
            lines.append(f"| {k} | {v} |")
        if not by_code:
            lines.append("| (none) | 0 |")
        lines += [
            "",
            "## 重播可行性",
            "",
            "- 歷史 `KLINE_WARMUP_SHORT` 記錄保留不刪；**以「目前歸檔狀態」判斷可否暖機重播**。",
            "- `klines_1m/` 為本 run 專用歸檔；滾動 vault 覆寫**不影響**已歸檔列。",
            "- `quote_events.jsonl` 僅含實際用於管理/進場的報價，**不**補造未收集的逐筆行情。",
            "- `fills.jsonl` / `position_events.jsonl` 於首次事件才建立（空檔＝本 run 尚無該類事件，非靜默丟棄）。",
            "",
            "## 最近事件（最多 50）",
            "",
        ]
        for e in events[-50:]:
            lines.append(f"- `{e.get('ts')}` **{e.get('code')}** {e.get('symbol')} {e.get('detail')} replay={e.get('replay_precise')}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def get_store() -> Optional[RunStore]:
    return _CURRENT


def begin_run(run_id: Optional[str] = None) -> RunStore:
    global _CURRENT
    rid = run_id or new_run_id()
    root = config.ROOT_OUT / "runs" / rid
    store = RunStore(run_id=rid, root=root)
    store.ensure()
    _CURRENT = store
    return store


def archive_legacy_root_into_run(label: str = "pre_overnight") -> Optional[Path]:
    """Copy current root artifacts into a frozen run folder (no overwrite of existing runs)."""
    rid = f"{label}_{new_run_id()}"
    dest = config.ROOT_OUT / "runs" / rid
    dest.mkdir(parents=True, exist_ok=False)
    for name in (
        "account_state.json",
        "run_manifest.json",
        "decision_trace.jsonl",
        "fills.jsonl",
        "trades.csv",
        "equity_snapshots.jsonl",
        "universe_snapshot.json",
        "review.md",
        "paper_stdout.log",
    ):
        src = config.ROOT_OUT / name
        if src.exists():
            shutil.copy2(src, dest / name)
    # snapshot rolling vault as-of archive time
    vault_dest = dest / "kline_vault_snapshot"
    if config.KLINE_VAULT_DIR.exists():
        shutil.copytree(config.KLINE_VAULT_DIR, vault_dest, dirs_exist_ok=True)
    atomic_write_json(
        dest / "archive_note.json",
        {
            "archived_at": now_iso(),
            "note": "Frozen copy of root outputs before overnight run_store; not a full observability run",
            "source_run_id_guess": "see run_manifest.json if present",
        },
    )
    return dest
