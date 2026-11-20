#!/usr/bin/env python3
"""Detach paper_bulk_test_v1 so agent/terminal exit cannot kill it."""
import os, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent / "code"
DATA = Path("/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root")
LOGDIR = Path("/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/launch_logs")
LOGDIR.mkdir(parents=True, exist_ok=True)
from datetime import datetime
from zoneinfo import ZoneInfo
stamp = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%dT%H%M%S")
log = LOGDIR / f"bulk_launch_{stamp}.log"
env = os.environ.copy()
env["BREAKOUT_RUNTIME_ROOT"] = str(DATA)
env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
cmd = [sys.executable, "-u", "-m", "breakout_follow_v1.paper_main", "--paper", "--profile", "paper_bulk_test_v1", "--runtime-root", str(DATA), "--hours", "0"]
fh = open(log, "w", buffering=1)
proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
print(f"pid={proc.pid}\nlog={log}")
