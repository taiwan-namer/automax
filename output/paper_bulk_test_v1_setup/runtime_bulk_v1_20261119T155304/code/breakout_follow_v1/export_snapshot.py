#!/usr/bin/env python3
"""
不中斷交易的一致性匯出：以固定截止時間打包本次 run。

用法：
  python3 -m breakout_follow_v1.export_snapshot
  python3 -m breakout_follow_v1.export_snapshot --run-id <id>
  python3 -m breakout_follow_v1.export_snapshot --cutoff 2026-11-19T08:00:00+08:00

不強制平倉、不清空帳戶、不改寫交易歷史；排除 .env / secret / token。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config
from .util import now_iso

SECRET_NAME_RE = re.compile(r"(api[_-]?key|secret|token|\.env|credential|password)", re.I)
TZ = ZoneInfo(config.TZ_NAME)


def _current_run_root() -> Path:
    ptr = config.ROOT_OUT / "current_run.json"
    if ptr.exists():
        data = json.loads(ptr.read_text(encoding="utf-8"))
        root = Path(data["root"])
        if root.exists():
            return root
    runs = config.ROOT_OUT / "runs"
    if runs.exists():
        dirs = sorted([p for p in runs.iterdir() if p.is_dir()], key=lambda p: p.name)
        if dirs:
            return dirs[-1]
    raise SystemExit("no run directory found")


def _parse_cutoff(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now(tz=TZ)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def _is_secret(path: Path) -> bool:
    return bool(SECRET_NAME_RE.search(path.name))


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        if _is_secret(src):
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        if _is_secret(p):
            continue
        rel = p.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)


def export_run(*, run_root: Path, cutoff: datetime, out_dir: Optional[Path] = None) -> Path:
    stamp = cutoff.astimezone(TZ).strftime("%Y%m%dT%H%M%S")
    run_id = run_root.name
    out_dir = out_dir or (config.ROOT_OUT / "exports")
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / f"{run_id}_cutoff_{stamp}"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    # freeze note — does not stop trader
    meta = {
        "exported_at": now_iso(),
        "cutoff": cutoff.isoformat(),
        "run_id": run_id,
        "run_root": str(run_root.resolve()),
        "note": "Consistent copy at cutoff wall-clock; trader may continue writing after this",
        "excludes": ["*.env", "*secret*", "*token*", "*api_key*", "*credential*"],
        "does_not": ["force_flat", "reset_account", "rewrite_history"],
    }
    (bundle / "export_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    _copy_tree(run_root, bundle / "run")
    # also include live account pointer for restore proof
    if config.ACCOUNT_PATH.exists():
        shutil.copy2(config.ACCOUNT_PATH, bundle / "account_live_at_export.json")

    # refresh data_quality.md into bundle if generator available
    dq = run_root / "data_quality.md"
    if dq.exists():
        shutil.copy2(dq, bundle / "run" / "data_quality.md")

    tar_path = out_dir / f"{bundle.name}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(bundle, arcname=bundle.name)

    manifest = {
        "bundle_dir": str(bundle.resolve()),
        "tarball": str(tar_path.resolve()),
        "export_meta": meta,
        "files": sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file()),
    }
    (bundle / "export_index.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return tar_path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Export breakout_follow_v1 run snapshot without stopping paper")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--cutoff", default=None, help="ISO time; default=now")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)

    if args.run_id:
        run_root = config.ROOT_OUT / "runs" / args.run_id
        if not run_root.exists():
            raise SystemExit(f"run not found: {run_root}")
    else:
        run_root = _current_run_root()

    cutoff = _parse_cutoff(args.cutoff)
    out = Path(args.out_dir) if args.out_dir else None
    export_run(run_root=run_root, cutoff=cutoff, out_dir=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
