"""Redirect ALL breakout_follow_v1 I/O paths into a temporary directory."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def isolate_all_paths(config: Any, tmp: Path) -> dict[str, Path]:
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    # 固定 RUNTIME_ROOT，避免相對路徑隨 cwd 漂移
    if hasattr(config, "set_runtime_root"):
        config.set_runtime_root(tmp)
    elif hasattr(config, "RUNTIME_ROOT"):
        config.RUNTIME_ROOT = tmp.resolve()

    root = tmp / "output" / "breakout_follow_v1"
    vault = tmp / "data" / "breakout_follow_v1" / "kline_vault_v2_clean"
    root.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    (root / "decision_snapshots").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)

    mapping = {
        "RUNTIME_ROOT": tmp.resolve(),
        "ROOT_OUT": root,
        "ACCOUNT_PATH": root / "account_state.json",
        "RUN_MANIFEST_PATH": root / "run_manifest.json",
        "DECISION_TRACE_PATH": root / "decision_trace.jsonl",
        "FILLS_PATH": root / "fills.jsonl",
        "TRADES_PATH": root / "trades.csv",
        "EQUITY_PATH": root / "equity_snapshots.jsonl",
        "REVIEW_PATH": root / "review.md",
        "UNIVERSE_PATH": root / "universe_snapshot.json",
        "PID_PATH": root / "paper.pid",
        "LOCK_PATH": root / "paper_profile.lock",
        "KLINE_VAULT_DIR": vault,
        "DECISION_SNAPSHOT_DIR": root / "decision_snapshots",
    }
    saved = {}
    for k, v in mapping.items():
        saved[k] = getattr(config, k, None)
        setattr(config, k, v)
    return saved


def restore_paths(config: Any, saved: dict[str, Path]) -> None:
    for k, v in saved.items():
        if v is None and hasattr(config, k):
            continue
        setattr(config, k, v)
