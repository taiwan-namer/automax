#!/usr/bin/env python3
"""正式切換作業腳本（安全閘門）。

預設：僅 --help / precheck-readonly。
真正停機／遷移／啟動必須加 --i-understand-deploy 且逐步確認。

本輪就緒檢查不得執行停機／遷移／啟動。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# …/automax/worktrees/breakout_follow_v1_fix_*/deploy_switch_ops.py → repo root = parents[2]
FIX_PKG = Path(__file__).resolve().parent
REPO = FIX_PKG.parents[1]
TZ = timezone(timedelta(hours=8))

ACCOUNT_PATH = REPO / "output/breakout_follow_v1/account_state.json"
FILLS_PATH = REPO / "output/breakout_follow_v1/fills.jsonl"
PID_PATH = REPO / "output/breakout_follow_v1/paper.pid"
ROOT_OUT = REPO / "output/breakout_follow_v1"
LIVE_PKG = REPO / "breakout_follow_v1"
ARTIFACTS = ROOT_OUT / "runs/20261119T024357_183e09f4/deploy_artifacts"
LOCK_PATH = ROOT_OUT / ".deploy_switch.lock"


class Abort(Exception):
    pass


def now_ts() -> str:
    return datetime.now(TZ).strftime("%Y%m%dT%H%M%S")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_cmd(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def read_account() -> dict[str, Any]:
    if not ACCOUNT_PATH.exists():
        raise Abort(f"必要帳戶檔不存在: {ACCOUNT_PATH}")
    return json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))


def positions_n(acc: Optional[dict] = None) -> int:
    acc = acc or read_account()
    return len(acc.get("positions") or {})


def list_paper_pids() -> list[int]:
    """以命令列搜尋，不依賴 pid 檔是否存在。"""
    try:
        out = run_cmd(["pgrep", "-f", "breakout_follow_v1.paper_main"], check=False)
    except FileNotFoundError:
        out = run_cmd(["ps", "ax", "-o", "pid=,command="], check=True)
        pids = []
        for line in out.stdout.splitlines():
            if "breakout_follow_v1.paper_main" in line and "grep" not in line:
                pids.append(int(line.strip().split(None, 1)[0]))
        return pids
    pids = []
    for tok in out.stdout.split():
        try:
            pids.append(int(tok))
        except ValueError:
            pass
    return pids


def process_identity(pid: int) -> dict[str, Any]:
    if not Path(f"/proc/{pid}").exists():
        # macOS: use ps
        ps = run_cmd(["ps", "-p", str(pid), "-o", "pid=,ppid=,etime=,command="], check=False)
        if ps.returncode != 0 or not ps.stdout.strip():
            raise Abort(f"PID {pid} 不存在或無法讀取（可能已退出或 PID 重用失效）")
        parts = ps.stdout.strip().split(None, 3)
        cmdline = parts[3] if len(parts) > 3 else ""
        cwd = ""
        lsof = run_cmd(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], check=False)
        for line in lsof.stdout.splitlines():
            if line.startswith("n"):
                cwd = line[1:]
                break
    else:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        cwd = os.readlink(f"/proc/{pid}/cwd")
    return {
        "pid": pid,
        "cmdline": cmdline,
        "cwd": cwd,
        "is_paper_main": "breakout_follow_v1.paper_main" in cmdline,
        "cwd_is_repo": Path(cwd).resolve() == REPO.resolve() if cwd else False,
    }


def assert_no_account_writers(*, allow_pids: Optional[set[int]] = None) -> None:
    allow_pids = allow_pids or set()
    # 1) 無 paper_main
    live = [p for p in list_paper_pids() if p not in allow_pids]
    if live:
        details = []
        for p in live:
            try:
                details.append(process_identity(p))
            except Abort as e:
                details.append({"pid": p, "error": str(e)})
        raise Abort(f"仍有 paper_main 進程: {json.dumps(details, ensure_ascii=False)}")
    # 2) 若 pid 檔存在，其 PID 必須已死，且不得僅憑檔案判斷「安全」
    if PID_PATH.exists():
        raw = PID_PATH.read_text(encoding="utf-8").strip()
        try:
            file_pid = int(raw)
        except ValueError as e:
            raise Abort(f"paper.pid 內容無效: {raw!r}") from e
        alive = run_cmd(["ps", "-p", str(file_pid), "-o", "pid="], check=False)
        if alive.returncode == 0 and alive.stdout.strip():
            ident = process_identity(file_pid)
            if ident["is_paper_main"]:
                raise Abort(f"paper.pid={file_pid} 對應進程仍在運行: {ident}")
            raise Abort(
                f"paper.pid={file_pid} 仍存活但命令列不符 paper_main（疑似 PID 重用）: {ident}"
            )
    # 3) lsof 帳戶檔寫入者
    lsof = run_cmd(["lsof", str(ACCOUNT_PATH)], check=False)
    writers = []
    for line in lsof.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2:
            try:
                wpid = int(cols[1])
            except ValueError:
                continue
            if wpid not in allow_pids and wpid != os.getpid():
                writers.append(line)
    if writers:
        raise Abort("account_state.json 仍有外部開啟者:\n" + "\n".join(writers))


def acquire_mutex(owner: str, *, require_no_writers: bool = True) -> None:
    """取得部署鎖。

    stop 階段必須允許既有 paper_main 存在（否則無法停機）；
    backup/install/migrate/start 則要求無帳戶寫入者。
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        meta = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        raise Abort(f"部署鎖已被占用: {meta}")
    if require_no_writers:
        assert_no_account_writers()
    payload = {
        "owner": owner,
        "pid": os.getpid(),
        "acquired_at": datetime.now(TZ).isoformat(),
        "require_no_writers": require_no_writers,
    }
    tmp = LOCK_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, LOCK_PATH)
    if require_no_writers:
        assert_no_account_writers()


def release_mutex() -> None:
    if LOCK_PATH.exists():
        LOCK_PATH.unlink()


def copy_required(src: Path, dst: Path) -> dict[str, str]:
    if not src.exists():
        raise Abort(f"必要檔案缺失: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        raise Abort(f"複製失敗 {src} → {dst}: {e}") from e
    hs, hd = sha256_file(src), sha256_file(dst)
    if hs != hd:
        raise Abort(f"備份 hash 不一致: {src} {hs} vs {dst} {hd}")
    return {"src": str(src), "dst": str(dst), "sha256": hs}


def copy_optional(src: Path, dst: Path, log: list[dict]) -> None:
    if not src.exists():
        log.append({"path": str(src), "status": "ABSENT_OPTIONAL"})
        return
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        raise Abort(f"可選檔存在但複製失敗: {src} → {dst}: {e}") from e
    hs, hd = sha256_file(src), sha256_file(dst)
    if hs != hd:
        raise Abort(f"可選檔備份 hash 不一致: {src}")
    log.append({"path": str(src), "status": "COPIED", "sha256": hs, "dst": str(dst)})


def backup_consistency(tag: str) -> Path:
    ts = now_ts()
    bk = ARTIFACTS / f"consistency_backup_{tag}_{ts}"
    bk.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"created_at": datetime.now(TZ).isoformat(), "required": [], "optional": []}
    for name in ("account_state.json", "fills.jsonl"):
        manifest["required"].append(copy_required(ROOT_OUT / name, bk / name))
    # trades.csv 若策略已產生則為必要對帳輔助；不存在則記錄
    copy_optional(ROOT_OUT / "trades.csv", bk / "trades.csv", manifest["optional"])
    for name in ("current_run.json", "run_manifest.json", "paper.pid", "equity_snapshots.jsonl", "decision_trace.jsonl"):
        copy_optional(ROOT_OUT / name, bk / name, manifest["optional"])
    (bk / "BACKUP_MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return bk


def stop_paper(*, timeout_sec: int = 120) -> dict[str, Any]:
    """有限等待；逾時回報，不自動 kill -9。停機前後都檢查持倉。"""
    before = positions_n()
    if before > 0:
        raise Abort(f"停機前持倉={before}：禁止強平／禁止繼續部署")
    pids = list_paper_pids()
    if not pids:
        # pid 檔殘留仍需驗證
        assert_no_account_writers()
        return {"stopped": True, "already_dead": True, "positions_before": 0, "positions_after": positions_n()}
    identities = [process_identity(p) for p in pids]
    for ident in identities:
        if not ident["is_paper_main"]:
            raise Abort(f"拒絕對非 paper_main 進程發訊號: {ident}")
        if not ident["cwd_is_repo"]:
            raise Abort(f"cwd 不符 repo，疑似 PID 重用或錯進程: {ident} expected_cwd={REPO}")
    for p in pids:
        os.kill(p, 15)  # SIGTERM
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        alive = list_paper_pids()
        if not alive:
            break
        time.sleep(1)
    else:
        raise Abort(
            f"停機逾時 {timeout_sec}s；仍存活 {list_paper_pids()}。"
            "不自動 kill -9。請人工處理後重跑。"
        )
    # 寫入穩定
    time.sleep(2)
    assert_no_account_writers()
    after = positions_n()
    if after > 0:
        raise Abort(
            f"CRITICAL: 停機後持倉={after}。持倉管理已中斷，禁止繼續部署／禁止強平。\n"
            f"安全恢復：1) 保留帳戶與 fills；2) 用與停機前相同程式碼與 vault 路徑重啟同一帳戶管理程序；\n"
            f"3) 確認 launch/manifest 後再管理持倉；4) 修復版切換延後至持倉歸零。"
        )
    return {"stopped": True, "identities": identities, "positions_before": before, "positions_after": after}


def install_code_from_seal(seal_dir: Path) -> dict[str, Any]:
    src = seal_dir / "breakout_follow_v1"
    if not src.is_dir():
        raise Abort(f"封存缺少 breakout_follow_v1/: {seal_dir}")
    # 備份 live 到唯一目錄
    live_bak = ARTIFACTS / f"live_pkg_backup_before_install_{now_ts()}"
    shutil.copytree(LIVE_PKG, live_bak / "breakout_follow_v1", ignore=shutil.ignore_patterns("__pycache__"))
    # 覆寫
    for p in LIVE_PKG.glob("*.py"):
        p.unlink()
    for p in src.glob("*.py"):
        shutil.copy2(p, LIVE_PKG / p.name)
    # verify hashes
    mismatched = []
    for p in src.glob("*.py"):
        if sha256_file(p) != sha256_file(LIVE_PKG / p.name):
            mismatched.append(p.name)
    if mismatched:
        raise Abort(f"安裝後 hash 不符: {mismatched}")
    return {"seal": str(seal_dir), "live_backup": str(live_bak)}


def migrate_live(*, reason: str) -> dict[str, Any]:
    sys.path.insert(0, str(FIX_PKG if (FIX_PKG / "breakout_follow_v1").exists() else REPO))
    # Prefer live package after install
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from breakout_follow_v1.account import AccountState, migrate_cash_gross_v1, save_account

    assert_no_account_writers()
    if positions_n() > 0:
        raise Abort("遷移前持倉非 0：中止")
    raw = ACCOUNT_PATH.read_bytes()
    src_hash = hashlib.sha256(raw).hexdigest()
    pre = ACCOUNT_PATH.with_name(f"account_state.pre_migration_{now_ts()}.json")
    shutil.copy2(ACCOUNT_PATH, pre)
    if sha256_file(ACCOUNT_PATH) != sha256_file(pre):
        raise Abort("遷移前備份 hash 失敗")
    st = AccountState.from_dict(json.loads(raw.decode()))
    before = {
        "cash_u": st.cash_u,
        "realized_net_u": st.realized_net_u,
        "realized_fees_u": st.realized_fees_u,
        "used_margin": st.used_margin(),
        "day_key": st.day_key,
        "day_new_entries_locked": st.day_new_entries_locked,
        "initial_capital_u": st.initial_capital_u,
    }
    meta = migrate_cash_gross_v1(st, source_hash=src_hash, reason=reason, dry_run=False)
    idem = migrate_cash_gross_v1(st, source_hash=src_hash, reason=reason + "_idem", dry_run=False)
    save_account(st)
    after = {
        "cash_u": st.cash_u,
        "realized_net_u": st.realized_net_u,
        "realized_fees_u": st.realized_fees_u,
        "used_margin": st.used_margin(),
        "day_key": st.day_key,
        "day_new_entries_locked": st.day_new_entries_locked,
        "initial_capital_u": st.initial_capital_u,
        "accounting_migration_version": st.accounting_migration_version,
    }
    out = ARTIFACTS / f"migration_apply_{now_ts()}.json"
    out.write_text(
        json.dumps(
            {"source_hash": src_hash, "pre_backup": str(pre), "before": before, "meta": meta, "idem": idem, "after": after},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {"report": str(out), "before": before, "after": after}


def start_paper(*, hours: float = 0.0) -> dict[str, Any]:
    assert_no_account_writers()
    if positions_n() > 0:
        raise Abort("啟動前持倉非 0：中止")
    # 確保乾淨 vault 目錄存在但不從 legacy 複製
    vault = REPO / "data/breakout_follow_v1/kline_vault_v2_clean"
    vault.mkdir(parents=True, exist_ok=True)
    log_path = ROOT_OUT / f"paper_stdout_{now_ts()}.log"
    cmd = [sys.executable, "-u", "-m", "breakout_follow_v1.paper_main", "--paper", "--hours", str(hours)]
    # 不覆寫舊日誌
    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # 等待 launch.json
    deadline = time.time() + 60
    launch = None
    run_root = None
    while time.time() < deadline:
        if not Path(f"/proc/{proc.pid}").exists():
            # macOS
            alive = run_cmd(["ps", "-p", str(proc.pid), "-o", "pid="], check=False)
            if alive.returncode != 0:
                raise Abort(f"新進程立即退出；見日誌 {log_path}")
        cur = ROOT_OUT / "current_run.json"
        if cur.exists():
            try:
                info = json.loads(cur.read_text(encoding="utf-8"))
                run_root = Path(info["root"])
                lj = run_root / "launch.json"
                if lj.exists():
                    launch = json.loads(lj.read_text(encoding="utf-8"))
                    if int(launch.get("pid") or -1) == proc.pid:
                        break
            except Exception:
                pass
        time.sleep(0.5)
    if not launch:
        raise Abort(f"60s 內未看到匹配 pid={proc.pid} 的 launch.json；日誌={log_path}")
    return {"pid": proc.pid, "log": str(log_path), "launch": launch, "run_root": str(run_root)}


def verify_running_from_launch() -> dict[str, Any]:
    """必須來自實際運行進程的 launch/manifest，不可只 import 磁碟套件。"""
    pids = list_paper_pids()
    if len(pids) != 1:
        raise Abort(f"期望恰好 1 個 paper_main，實際={pids}")
    pid = pids[0]
    ident = process_identity(pid)
    if not ident["is_paper_main"] or not ident["cwd_is_repo"]:
        raise Abort(f"運行進程身份不符: {ident}")
    cur = json.loads((ROOT_OUT / "current_run.json").read_text(encoding="utf-8"))
    run_root = Path(cur["root"])
    launch = json.loads((run_root / "launch.json").read_text(encoding="utf-8"))
    if int(launch["pid"]) != pid:
        raise Abort(f"launch.pid={launch['pid']} != 活進程 {pid}")
    if Path(launch["cwd"]).resolve() != REPO.resolve():
        raise Abort(f"launch.cwd 不符: {launch['cwd']}")
    man = json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))
    code_hashes = json.loads((run_root / "code_hashes.json").read_text(encoding="utf-8"))
    acct_start = run_root / "account_at_start.json"
    code_snap = run_root / "code_snapshot" / "breakout_follow_v1"
    if not acct_start.exists():
        raise Abort("缺少不可變 account_at_start.json")
    if not code_snap.is_dir():
        raise Abort("缺少 code_snapshot/breakout_follow_v1")
    # 比對運行中 package_dir 與 code_snapshot / live 檔 hash（以 launch 記錄為準）
    pkg = Path(launch["package_dir"])
    sample = "config.py"
    live_h = sha256_file(pkg / sample)
    snap_h = sha256_file(code_snap / sample)
    recorded = code_hashes["files"].get(f"breakout_follow_v1/{sample}") or code_hashes["files"].get(sample)
    if live_h != snap_h:
        raise Abort(f"運行 package 與 code_snapshot 不一致: {sample}")
    if recorded and recorded != live_h:
        raise Abort(f"code_hashes 與運行檔不一致: {sample}")
    if launch.get("code_fix_tag") != "fix_20261119_p0p1_trail_after_partial":
        raise Abort(f"launch.code_fix_tag 不符: {launch.get('code_fix_tag')}")
    if "kline_vault_v2_clean" not in str(launch.get("kline_vault_dir")):
        raise Abort(f"launch.kline_vault_dir 不符乾淨 vault: {launch.get('kline_vault_dir')}")
    return {
        "pid": pid,
        "identity": ident,
        "launch": launch,
        "manifest_run_id": man["run_id"],
        "code_fix_tag": launch.get("code_fix_tag"),
        "account_at_start": str(acct_start),
        "code_snapshot": str(code_snap),
        "ok": True,
    }


def cmd_precheck_readonly() -> int:
    acc = read_account()
    pids = list_paper_pids()
    idents = []
    for p in pids:
        try:
            idents.append(process_identity(p))
        except Abort as e:
            idents.append({"pid": p, "error": str(e)})
    report = {
        "ts": datetime.now(TZ).isoformat(),
        "positions": positions_n(acc),
        "day_locked": acc.get("day_new_entries_locked"),
        "day_key": acc.get("day_key"),
        "cash_u": acc.get("cash_u"),
        "pids": pids,
        "identities": idents,
        "note": "readonly; no stop/migrate/start",
    }
    out = ARTIFACTS / f"precheck_readonly_{now_ts()}.json"
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"wrote {out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="breakout_follow_v1 deploy switch ops")
    ap.add_argument("action", choices=[
        "precheck-readonly",
        "stop",
        "backup",
        "install-code",
        "migrate",
        "start",
        "verify-running",
        "full-switch",
    ])
    ap.add_argument("--i-understand-deploy", action="store_true", help="允許停機/遷移/啟動")
    ap.add_argument("--seal", type=str, default="", help="fix_source_sealed_* 目錄")
    ap.add_argument("--hours", type=float, default=0.0)
    ap.add_argument("--stop-timeout", type=int, default=120)
    args = ap.parse_args(argv)

    if args.action == "precheck-readonly":
        return cmd_precheck_readonly()

    # verify-running 為唯讀驗收，不需部署旗標
    if args.action == "verify-running":
        try:
            print(json.dumps(verify_running_from_launch(), indent=2, ensure_ascii=False))
            return 0
        except Abort as e:
            print(f"ABORT: {e}", file=sys.stderr)
            return 1

    if not args.i_understand_deploy:
        print("REFUSED: 需要 --i-understand-deploy 才可執行變更動作", file=sys.stderr)
        return 2

    try:
        if args.action == "stop":
            # 停機前允許既有 paper_main；停機函式內會驗證身分後 SIGTERM
            acquire_mutex("stop", require_no_writers=False)
            try:
                print(json.dumps(stop_paper(timeout_sec=args.stop_timeout), indent=2, ensure_ascii=False))
            finally:
                release_mutex()
        elif args.action == "backup":
            acquire_mutex("backup", require_no_writers=True)
            try:
                assert_no_account_writers()
                print(json.dumps({"backup": str(backup_consistency("manual"))}, indent=2))
            finally:
                release_mutex()
        elif args.action == "install-code":
            if not args.seal:
                raise Abort("--seal 必填")
            acquire_mutex("install-code", require_no_writers=True)
            try:
                assert_no_account_writers()
                print(json.dumps(install_code_from_seal(Path(args.seal)), indent=2))
            finally:
                release_mutex()
        elif args.action == "migrate":
            acquire_mutex("migrate", require_no_writers=True)
            try:
                print(json.dumps(migrate_live(reason="deploy_switch_apply"), indent=2, default=str))
            finally:
                release_mutex()
        elif args.action == "start":
            acquire_mutex("start", require_no_writers=True)
            try:
                print(json.dumps(start_paper(hours=args.hours), indent=2, ensure_ascii=False))
            finally:
                release_mutex()
        elif args.action == "full-switch":
            if not args.seal:
                raise Abort("--seal 必填")
            acquire_mutex("full-switch", require_no_writers=False)
            try:
                if positions_n() > 0:
                    raise Abort("full-switch 前持倉非 0")
                stop_info = stop_paper(timeout_sec=args.stop_timeout)
                assert_no_account_writers()
                bk = backup_consistency("post_stop")
                inst = install_code_from_seal(Path(args.seal))
                mig = migrate_live(reason="deploy_switch_apply")
                start_info = start_paper(hours=args.hours)
            finally:
                release_mutex()
            ver = verify_running_from_launch()
            print(json.dumps({"stop": stop_info, "backup": str(bk), "install": inst, "migrate": mig, "start": start_info, "verify": ver}, indent=2, default=str))
    except Abort as e:
        print(f"ABORT: {e}", file=sys.stderr)
        if LOCK_PATH.exists():
            try:
                release_mutex()
            except Exception:
                pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
