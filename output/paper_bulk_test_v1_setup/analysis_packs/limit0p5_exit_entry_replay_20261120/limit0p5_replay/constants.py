from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
CUTOFF = datetime.fromisoformat("2026-11-20T08:16:25+08:00")
# Vault / process may continue past cutoff; never silently mix into primary metrics.
EXT_OBS_END = datetime.fromisoformat("2026-11-20T09:03:00+08:00")

FEE_RATE_ONE_SIDE = 0.0005
SLIPPAGE_ADVERSE = 0.0002

RUNS_B = frozenset({"20261120T040855_67e68885", "20261120T042025_ab76e26a"})
RUNS_C = frozenset({"20261120T040855_cfc21db0", "20261120T042025_b9dc1424"})

PROFILE_B = "paper_reverse_fixed_usdt_50_75_v1"
PROFILE_C = "paper_reverse_fixed_usdt_50_75_inv_v1"

TP_GRID = (50.0, 100.0, 150.0, 200.0)
SL_GRID = (75.0, 50.0, 35.0, 25.0)

BASELINE_TP = 50.0
BASELINE_SL = 75.0

# Daily-report reconstruction targets (primary cutoff).
EXPECTED_B = {"n_closed": 35, "n_open": 1, "sum_net": -146.1378750087361}
EXPECTED_C = {"n_closed": 36, "n_open": 8, "sum_net": -451.2702686223841}
