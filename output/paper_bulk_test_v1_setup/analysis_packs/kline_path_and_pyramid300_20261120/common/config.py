from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
CUTOFF = datetime.fromisoformat("2026-11-20T08:16:25+08:00")
EXT_END = datetime.fromisoformat("2026-11-20T09:03:00+08:00")

FEE = 0.0005
SLIP = 0.0002
LEVERAGE = 10.0
ORIG_MARGIN = 1000.0
ORIG_NOTIONAL = 10000.0
LIMIT_OFFSET = 0.005
CANCEL_ADVERSE = 0.01

# Pyramid primary: 300 U margin × 10x ≈ 3000 U notional
PY_MARGIN = 300.0
PY_NOTIONAL = PY_MARGIN * LEVERAGE  # 3000
PY_ADD_PCT = 0.005

RUNS_B = frozenset({"20261120T040855_67e68885", "20261120T042025_ab76e26a"})
RUNS_C = frozenset({"20261120T040855_cfc21db0", "20261120T042025_b9dc1424"})
PROFILE_B = "paper_reverse_fixed_usdt_50_75_v1"
PROFILE_C = "paper_reverse_fixed_usdt_50_75_inv_v1"

PACK_ROOT = Path(__file__).resolve().parents[1]
SETUP_ROOT = PACK_ROOT.parents[1]
PREV_PACK = SETUP_ROOT / "analysis_packs" / "limit0p5_exit_entry_replay_20261120"


def account_dir(profile: str) -> Path:
    return SETUP_ROOT / "fixed_data_root" / "output" / profile


def vault_dir(profile: str) -> Path:
    return SETUP_ROOT / "fixed_data_root" / "data" / profile / "kline_vault_v2_clean"
