"""Periodic review.md for breakout_follow_v1."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from . import config
from .util import now_iso


def _read_jsonl(path: Path, limit: int = 200000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def write_review(
    st,
    last_stats: dict[str, Any],
    manifest: dict[str, Any],
    *,
    store=None,
) -> Path:
    dec_path = store.path("decision_trace.jsonl") if store else config.DECISION_TRACE_PATH
    fill_path = store.path("fills.jsonl") if store else config.FILLS_PATH
    eq_path = store.path("equity_snapshots.jsonl") if store else config.EQUITY_PATH

    decisions = _read_jsonl(dec_path)
    fills = _read_jsonl(fill_path)
    equity = _read_jsonl(eq_path)

    opens = [f for f in fills if f.get("event") == "OPEN"]
    partials = [f for f in fills if f.get("event") == "CLOSE_PARTIAL"]
    closes = [f for f in fills if f.get("event") == "CLOSE"]
    reject_c = Counter()
    for d in decisions:
        fr = d.get("first_reject_code") or d.get("block_reason")
        if fr and d.get("final_action") in (None, "NO_ENTRY", "BLOCKED"):
            reject_c[str(fr)] += 1

    nets = [
        float(f.get("net_u") or f.get("realized_net_u") or 0)
        for f in fills
        if f.get("event") in ("CLOSE", "CLOSE_PARTIAL")
    ]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross = sum(n for n in nets if n > 0)
    loss_abs = abs(sum(n for n in nets if n < 0))
    pf = (gross / loss_abs) if loss_abs > 0 else float("inf") if gross > 0 else 0.0
    fees = float(st.realized_fees_u)
    realized = float(st.realized_net_u)

    last_eq = equity[-1] if equity else {}
    floating = float(last_eq.get("floating_u") or last_eq.get("floating") or 0.0)
    equity_now = float(last_eq.get("equity") or (st.initial_capital_u + realized + floating))

    dd = 0.0
    peak = None
    for e in equity:
        eq = float(e.get("equity") or 0)
        if peak is None or eq > peak:
            peak = eq
        if peak is not None:
            dd = min(dd, eq - peak)

    pid = "UNKNOWN"
    if config.PID_PATH.exists():
        pid = config.PID_PATH.read_text(encoding="utf-8").strip()

    cov = last_stats.get("coverage") or {}
    run_dir = str(store.root) if store else str(config.ROOT_OUT)
    # 對帳：fills net 合計 vs 帳戶 realized（允許開倉費差異口徑；僅報告）
    fill_net_sum = sum(nets) if nets else 0.0
    peak_positions = max((int(e.get("positions") or 0) for e in equity), default=len(st.positions))
    used_margin = float(st.used_margin()) if hasattr(st, "used_margin") else 0.0
    capital_use = (used_margin / float(st.initial_capital_u)) if st.initial_capital_u else 0.0
    closed_n = len(closes)
    win_rate = (len(wins) / closed_n) if closed_n else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    payoff = (abs(avg_win / avg_loss) if (avg_win is not None and avg_loss not in (None, 0)) else None)

    by_symbol = Counter(f.get("symbol") for f in closes)
    by_side = Counter(f.get("side") for f in closes)
    by_exit = Counter(f.get("exit_reason") for f in closes)
    hold_secs = [float(f.get("hold_sec") or 0) for f in closes if f.get("hold_sec") is not None]
    avg_hold = (sum(hold_secs) / len(hold_secs)) if hold_secs else None

    lines = [
        f"# {getattr(config, 'PROFILE_ID', config.STRATEGY_ID)} review",
        "",
        f"> 更新時間：{now_iso()}（{config.TZ_NAME}）",
        f"> profile：`{getattr(config, 'PROFILE_ID', config.STRATEGY_ID)}`｜strategy_version：`{config.STRATEGY_VERSION}`｜run_id：`{st.run_id}`｜PID：`{pid}`",
        f"> code_fix_tag：`{getattr(config, 'CODE_FIX_TAG', '')}`｜config_hash：`{manifest.get('config_hash')}`（algo `{config.CONFIG_HASH_ALGO}`）",
        f"> run_dir：`{run_dir}`",
        "",
        "## 程序與資料",
        "",
        f"- 帳戶：`{config.ACCOUNT_PATH}`｜初始本金 {st.initial_capital_u}",
        f"- **已實現淨損益**：{realized:+.4f} U｜**累計費用**：{fees:.4f} U｜**現金**：{st.cash_u:.4f} U",
        f"- **浮動損益（最近快照）**：{floating:+.4f} U｜**權益（含浮盈）**：{equity_now:.4f} U",
        f"- 持倉數：{len(st.positions)}／上限 {config.MAX_CONCURRENT_POSITIONS}｜峰值持倉（快照）：{peak_positions}｜保證金使用率：{capital_use:.2%}",
        f"- 日損鎖定：{st.day_new_entries_locked}｜日起始權益：{st.day_start_equity_u}｜日損上限：{config.DAILY_LOSS_LOCK_USDT}",
        f"- 合格標的：{len(st.universe)}｜漲幅前{config.TOP_N}：{len(st.top_gainers)}｜跌幅前{config.TOP_N}：{len(st.top_losers)}",
        f"- 最近掃描覆蓋：expected={cov.get('expected')} success={cov.get('success')} failed={cov.get('failed')} — **失敗者不計已掃**",
        f"- 最近掃描耗時：{last_stats.get('duration_sec')}s｜候選 {last_stats.get('candidates')}｜開倉 {last_stats.get('opened')}",
        f"- 資金費：`funding_data_gap={st.funding_data_gap}`（缺口時不得冒充完整淨收益）",
        f"- 滑價假設每側 {config.SLIPPAGE_ADVERSE}｜手續費假設每側 {config.FEE_RATE_ONE_SIDE}（SIMULATION_ASSUMPTION）",
        f"- fills 分段淨損益合計：{fill_net_sum:+.4f} U（對帳參考；與帳戶 realized 口徑見成本說明）",
        "",
        "## 拒絕分布（decision_trace）",
        "",
    ]
    if reject_c:
        lines.append("| reason | n |")
        lines.append("|---|---:|")
        for k, v in reject_c.most_common(30):
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("_尚無拒絕紀錄_")

    def _fmt_ratio(x):
        if x is None:
            return "n/a（樣本不足）"
        if x == float("inf"):
            return "inf"
        return f"{x:.4f}"

    lines += [
        "",
        "## 成交統計（模擬）",
        "",
        f"- OPEN={len(opens)} PARTIAL_TP={len(partials)} CLOSE={len(closes)}",
        f"- 已實現淨損益 {realized:+.4f} U（含費用已扣）｜費用合計 {fees:.4f} U",
        f"- 勝率={_fmt_ratio(win_rate)}｜平均盈={_fmt_ratio(avg_win)}｜平均虧={_fmt_ratio(avg_loss)}｜盈虧比={_fmt_ratio(payoff)}｜PF={_fmt_ratio(pf if pf != float('inf') else float('inf'))}",
        f"- 分段淨損益 wins={len(wins)} losses={len(losses)}｜每筆期望={(realized/len(nets) if nets else 0):+.4f}",
        f"- 平均持有秒數={_fmt_ratio(avg_hold)}（僅有 hold_sec 欄位之 CLOSE）",
        f"- 權益曲線含浮盈最大回撤（快照）：{dd:+.4f} U",
        "",
        "### 分組（CLOSE）",
        "",
        f"- 按標的：{dict(by_symbol.most_common(20)) or '{}'}",
        f"- 按多空：{dict(by_side) or '{}'}",
        f"- 按退出原因：{dict(by_exit) or '{}'}",
        "",
        "## 持倉",
        "",
    ]
    if not st.positions:
        lines.append("_目前無持倉_")
    else:
        lines.append("| symbol | side | qty_rem | entry | stop_eff | partial | trade_id |")
        lines.append("|---|---|---:|---:|---:|---|---|")
        for sym, p in st.positions.items():
            lines.append(
                f"| {sym} | {p.get('side')} | {p.get('qty_remaining')} | {p.get('entry_price')} | "
                f"{p.get('effective_stop')} | {p.get('partial_tp_done')} | `{p.get('trade_id')}` |"
            )

    lines += [
        "",
        "## 資料檔",
        "",
        f"- decision_trace / fills / equity / universe_history / quote_events / stop_events / klines_1m / data_quality.md",
        f"- 匯出：`python3 -m breakout_follow_v1.export_snapshot --run-id {st.run_id}`",
        "",
        "## 模擬限制",
        "",
        "- 成交為公開 bid/ask + 不利滑價假設，**非**交易所實際成交。",
        "- 未收集的逐筆行情**不會**補造。",
        "- 參數首版預設，**未**最佳化，**不**保證獲利。",
        "",
    ]
    text = "\n".join(lines) + "\n"
    config.REVIEW_PATH.write_text(text, encoding="utf-8")
    if store is not None:
        store.path("review.md").write_text(text, encoding="utf-8")
        return store.path("review.md")
    return config.REVIEW_PATH
