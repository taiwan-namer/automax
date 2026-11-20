"""Run tests/breakout_follow_v1/test_breakout_core.py with cwd = test_sandbox/ so that the
relative config.ROOT_OUT ("output/breakout_follow_v1") resolves inside the sandbox and never
touches the live output directory. Each test is called individually; failures are recorded."""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SB = Path(__file__).resolve().parent / "test_sandbox"
SB.mkdir(exist_ok=True)
os.chdir(SB)
sys.path.insert(0, str(REPO))

spec = importlib.util.spec_from_file_location("tbc", REPO / "tests/breakout_follow_v1/test_breakout_core.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []
for name in sorted(n for n in dir(mod) if n.startswith("test_")):
    try:
        getattr(mod, name)()
        results.append((name, "PASSED", ""))
    except Exception as e:  # noqa: BLE001
        results.append((name, "FAILED", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))

for n, s, msg in results:
    print(f"{s:7s} {n}")
    if msg:
        print("    " + msg.replace("\n", "\n    "))
print(f"cwd={SB}")
print("files written in sandbox:")
for p in sorted(SB.rglob("*")):
    if p.is_file():
        print("  ", p.relative_to(SB), p.stat().st_size)
