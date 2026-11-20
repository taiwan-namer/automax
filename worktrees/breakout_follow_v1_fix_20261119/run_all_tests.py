#!/usr/bin/env python3
"""Run all fix-validation tests without pytest; every test gets a fresh tmp dir; no network."""
from __future__ import annotations

import importlib
import inspect
import io
import json
import os
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
from pathlib import Path

FIX = Path(__file__).resolve().parent
REPO = FIX.parents[1]
sys.path.insert(0, str(FIX))
os.chdir(FIX)  # relative paths inside ensure_dirs resolve under worktree if any leak

TZ = timezone(timedelta(hours=8))
OUT = FIX / "test_results"
OUT.mkdir(exist_ok=True)
(OUT / "tmp").mkdir(exist_ok=True)


def run_module(mod_name: str) -> list[dict]:
    mod = importlib.import_module(mod_name)
    results = []
    for name, fn in sorted(inspect.getmembers(mod, inspect.isfunction)):
        if not name.startswith("test_"):
            continue
        tmp = Path(tempfile.mkdtemp(prefix=f"bfv1_{name}_", dir=str(OUT / "tmp")))
        buf = io.StringIO()
        status = "PASSED"
        err = ""
        try:
            with redirect_stdout(buf):
                sig = inspect.signature(fn)
                if "tmp_path" in sig.parameters:
                    fn(tmp)
                else:
                    fn()
        except Exception as e:
            status = "FAILED"
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=8)}"
        results.append(
            {
                "module": mod_name,
                "test": name,
                "status": status,
                "tmp": str(tmp),
                "stdout": buf.getvalue(),
                "error": err,
            }
        )
        print(f"{status:7s} {mod_name}.{name}")
        if err:
            print(err)
    return results


def main():
    modules = [
        "tests.test_breakout_core",
        "tests.test_klines_merge",
        "tests.test_extreme_trail",
        "tests.test_trail_after_partial_policy",
        "tests.test_cash_accounting",
        "tests.test_reverse_engine",
        "tests.test_safety_and_isolation",
        "tests.test_dedup_semantics",
        "tests.test_daily_loss_lock_2000",
        "tests.test_paper_bulk_test_v1",
        "tests.test_fill_account_reconciliation",
        "tests.test_http_reliability",
        "tests.test_manage_isolation_and_stale",
        "tests.test_bulk_reverse_policy",
    ]
    all_rows = []
    for m in modules:
        # fresh import each time to avoid path pollution across modules
        if m in sys.modules:
            del sys.modules[m]
        all_rows.extend(run_module(m))
    summary = {
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        "passed": sum(1 for r in all_rows if r["status"] == "PASSED"),
        "failed": sum(1 for r in all_rows if r["status"] == "FAILED"),
        "total": len(all_rows),
        "results": all_rows,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [f"{r['status']}\t{r['module']}.{r['test']}" + (f"\n{r['error']}" if r["error"] else "") for r in all_rows]
    (OUT / "summary.txt").write_text(
        f"passed={summary['passed']} failed={summary['failed']} total={summary['total']}\n" + "\n".join(lines),
        encoding="utf-8",
    )
    print(f"\nSUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    print(f"wrote {OUT / 'summary.json'}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
