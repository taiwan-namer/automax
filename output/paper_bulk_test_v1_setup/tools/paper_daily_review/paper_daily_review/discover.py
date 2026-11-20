from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_PROFILE_ID = "paper_bulk_test_v1"


@dataclass
class SetupPaths:
    setup_root: Path
    account_root: Path
    runs_dir: Path
    vault_dir: Path
    analysis_packs: Path
    daily_reports: Path
    account_state: Path
    root_fills: Path
    root_equity: Path
    root_trades_csv: Path
    profile_id: str = DEFAULT_PROFILE_ID
    current_run_id: Optional[str] = None
    current_run_dir: Optional[Path] = None
    launch: Dict[str, Any] = field(default_factory=dict)
    code_hashes: Dict[str, Any] = field(default_factory=dict)
    live_pid_file: Optional[str] = None
    live_cmdline_ok: Optional[bool] = None
    notes: List[str] = field(default_factory=list)


def discover_setup(setup_root: Path, profile_id: str = DEFAULT_PROFILE_ID) -> SetupPaths:
    """依 profile 綁定帳戶與報告輸出；不同 profile 不得共用 daily_reports 目錄。"""
    setup_root = setup_root.resolve()
    profile_id = str(profile_id or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
    account_root = setup_root / "fixed_data_root" / "output" / profile_id
    vault = setup_root / "fixed_data_root" / "data" / profile_id / "kline_vault_v2_clean"
    analysis = setup_root / "analysis_packs"
    # 舊 bulk 維持原路徑；其他 profile 獨立子目錄，避免覆寫／混算
    if profile_id == DEFAULT_PROFILE_ID:
        daily = analysis / "daily_reports"
    else:
        daily = analysis / f"daily_reports_{profile_id}"
    paths = SetupPaths(
        setup_root=setup_root,
        account_root=account_root,
        runs_dir=account_root / "runs",
        vault_dir=vault,
        analysis_packs=analysis,
        daily_reports=daily,
        account_state=account_root / "account_state.json",
        root_fills=account_root / "fills.jsonl",
        root_equity=account_root / "equity_snapshots.jsonl",
        root_trades_csv=account_root / "trades.csv",
        profile_id=profile_id,
    )
    cur = account_root / "current_run.json"
    if cur.exists():
        try:
            c = json.loads(cur.read_text())
            paths.current_run_id = c.get("run_id")
            if c.get("root"):
                paths.current_run_dir = Path(c["root"])
            elif paths.current_run_id:
                paths.current_run_dir = paths.runs_dir / paths.current_run_id
        except Exception as exc:
            paths.notes.append(f"current_run.json parse fail: {exc}")
    if paths.current_run_dir and (paths.current_run_dir / "launch.json").exists():
        paths.launch = json.loads((paths.current_run_dir / "launch.json").read_text())
    if paths.current_run_dir and (paths.current_run_dir / "code_hashes.json").exists():
        paths.code_hashes = json.loads((paths.current_run_dir / "code_hashes.json").read_text())
    pid_path = account_root / "paper.pid"
    if pid_path.exists():
        paths.live_pid_file = pid_path.read_text().strip()
        # identity check is best-effort; caller may verify cmdline
    return paths


def list_run_dirs(paths: SetupPaths) -> List[Path]:
    if not paths.runs_dir.exists():
        return []
    return sorted([p for p in paths.runs_dir.iterdir() if p.is_dir()], key=lambda p: p.name)


def inventory_fields() -> Dict[str, Any]:
    return {
        "available": [
            "fills.jsonl (root+runs): OPEN/CLOSE/CLOSE_PARTIAL, fees, net_u, trade_id, run_id, stops, tags",
            "equity_snapshots.jsonl: equity/cash/realized/floating/positions snapshots",
            "account_state.json: live cash/realized/positions (NOT midnight unless coincidentally)",
            "quote_events.jsonl (per run): pre-manage quote batches MANAGE/MANAGE_POLL",
            "data_quality.jsonl: HTTP_TIMEOUT/MANAGE_POLL_ERROR/429/418 notes",
            "launch.json + code_hashes.json: seal identity",
            "klines_1m / vault: 1m OHLC for QA/derived HTF",
            "review.md: human snapshot (event-mix; not C complete)",
            "prior analysis_packs summaries: reusable A/B/C/D framing",
        ],
        "missing_or_na": [
            "midnight account snapshot → day A delta may use nearest equity snap + lag",
            "manage completion events (NOOP/FILLED/…) → manage-complete interval = NA until deployed",
            "exchange event timestamps on quotes → fetched_at is local receipt time",
            "tick path → cannot claim true first stop pierce time",
            "complete funding → funding_data_gap",
        ],
        "reusable": [
            "analysis_packs/analysis_20261119_chatgpt_replay trade_id complete_net merge rules",
            "stage2 pre-manage quote batch interval methodology",
            "MGMT design docs (not code)",
        ],
    }
