from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import SCHEMA_VERSION, __version__
from .discover import SetupPaths, discover_setup, inventory_fields, list_run_dirs
from .jsonl_io import FileLock, atomic_write_json, atomic_write_text, read_jsonl_bounded
from .kline_qa import analyze_kline_qa
from .manage_health import analyze_manage_health
from .strategy_candidates import build_strategy_candidates
from .timeutil import TAIPEI, day_window, parse_iso
from .trades import (
    account_day_metrics,
    analyze_day_trades,
    complete_trade_net,
    group_by_trade_id,
    load_all_fills,
)


def _zh_char_count(s: str) -> int:
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")


def _fmt(x: Any, nd: int = 2) -> str:
    if x is None:
        return "NA"
    if isinstance(x, float):
        if abs(x) >= 100 or abs(x) < 0.01:
            return f"{x:.4f}"
        return f"{x:.{nd}f}"
    return str(x)


def _fmt_ts(x: Any) -> str:
    if not x:
        return "NA"
    s = str(x)
    return s.replace("+08:00", "")[:19] if "T" in s else s


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _completeness(
    start: datetime,
    end: datetime,
    asof: datetime,
    fills_meta: Dict[str, Any],
    first_fill: Optional[str],
    last_fill: Optional[str],
) -> Dict[str, Any]:
    day_done = asof >= end
    if not day_done:
        status = "部分日／試產"
        reason = f"截止 asof={asof.isoformat()} 早於日界 {end.isoformat()}；交易仍可能進行中"
    elif fills_meta.get("truncated_tails"):
        status = "部分日／資料尾截斷警告"
        reason = "JSONL 末行不完整已延後；其餘視為可讀"
    else:
        status = "完整日"
        reason = "asof 已過日界且來源讀取無截斷尾"
    return {
        "status": status,
        "reason": reason,
        "window": [start.isoformat(), end.isoformat()],
        "asof": asof.isoformat(),
        "first_fill_in_sources": first_fill,
        "last_fill_in_sources": last_fill,
        "truncated_tails": fills_meta.get("truncated_tails") or [],
    }


def build_daily_summary_md(ctx: Dict[str, Any]) -> str:
    c = ctx
    comp = c["completeness"]
    A = c["metrics_a"]
    B = c["day_trades"].metrics_b
    Cm = c["day_trades"].metrics_c
    D = c["day_trades"].metrics_d
    mh = c["manage_health"]
    kq = c["kline_qa"]
    cands = c["candidates"]
    strat = c["strategy_identity"]
    hyp = c.get("hypotheses_track") or []

    lines: List[str] = []
    lines.append(f"# 每日復盤 {c['day']}（Asia/Taipei）")
    lines.append("")
    lines.append(f"- 工具版本：`{__version__}`／schema `{SCHEMA_VERSION}`")
    lines.append(f"- 截止 asof：`{comp['asof']}`")
    lines.append(f"- 視窗：`{comp['window'][0]}` ≤ t < `{comp['window'][1]}`")
    lines.append(f"- 完整性：**{comp['status']}** — {comp['reason']}")
    lines.append("")
    lines.append("## 1. 結論（3–5 句）")
    for s in c["conclusions"]:
        lines.append(f"- {s}")
    lines.append("")
    lines.append(
        "本節僅描述截至 asof 的可觀測結果。部分日不得外推為全日期望；"
        "完整日需待日界結束後以相同工具重跑。任何策略或管理修復皆未因本報表自動生效。"
    )
    lines.append("")
    lines.append("## 2. 當時實際策略（提案／實作／部署分離）")
    lines.append(
        "實際運行身分以 launch／code_hashes／fills 內欄位為準；"
        "`current_run.json` 與 `paper.pid` 只是檔案指標，"
        "不能單獨證明進程仍存活或指令列未變。沙箱內 `ps` 結果亦不可作為唯一依據。"
    )
    lines.append(f"- profile：`{strat.get('profile_id')}`")
    if strat.get("account_root"):
        lines.append(f"- 帳戶根：`{strat.get('account_root')}`")
    if strat.get("daily_reports_root"):
        lines.append(f"- 報告根：`{strat.get('daily_reports_root')}`（與其他 profile 隔離）")
    exp = c.get("experiment_meta") or {}
    if exp:
        lines.append("")
        label = exp.get("experiment_label") or exp.get("profile_id") or "紙上實驗"
        lines.append(f"## 0. 實驗覆蓋／日損口徑（{label}）")
        lines.append(f"- 實驗啟動：`{exp.get('experiment_start')}`")
        if exp.get("exit_mode"):
            lines.append(f"- 退出模式：`{exp.get('exit_mode')}`")
        if exp.get("fixed_gross_enabled"):
            lines.append(
                f"- 固定毛退出：TP `{exp.get('fixed_gross_tp_usdt')}`／SL `{exp.get('fixed_gross_sl_usdt')}`"
            )
        if exp.get("mirror_only_entries") or exp.get("mirror_source_profile"):
            lines.append(
                f"- 鏡像進場：來源 `{exp.get('mirror_source_profile')}`｜"
                f"`{exp.get('mirror_entry_status')}`"
            )
        lines.append(f"- 結算界：`{exp.get('settlement_boundary')}`｜產報：`{exp.get('report_schedule')}`")
        lines.append(f"- 報告視窗：`{exp.get('report_window_start')}` ≤ t < `{exp.get('report_window_end')}`")
        lines.append(f"- **實際覆蓋**：`{exp.get('coverage_start')}` ≤ t < `{exp.get('coverage_end')}`（{exp.get('coverage_note')}）")
        lines.append(f"- 初始本金：`{exp.get('initial_capital_u')}`（注入≠盈利）")
        lines.append(f"- 日損上限：`{exp.get('daily_loss_lock_usdt')}`／日曆午夜切日；**不是**整場測試累計虧損上限")
        for w in exp.get("calendar_day_loss_windows") or []:
            lines.append(f"  - `{w.get('calendar_day')}`：{w.get('note')}")
        lines.append(f"- 新開倉截止：`{exp.get('new_entries_until_iso')}`｜程式閘門：`{exp.get('observation_cutoff_status')}`")
        lines.append(f"- SHORT→LONG 成交驗證：`{exp.get('short_to_long_fill_status')}`")
        lines.append(f"- LONG→SHORT 成交驗證：`{exp.get('long_to_short_fill_status')}`")
        lines.append(
            f"- 帳戶快照：本金 `{exp.get('initial_capital_u')}`｜已實現淨 `{exp.get('realized_net_u')}`｜"
            f"未實現 `{exp.get('unrealized_u')}`｜權益 `{exp.get('equity_u')}`｜"
            f"保證金占用 `{exp.get('margin_used_u')}`（≠虧損）｜現金 `{exp.get('cash_u')}`"
        )
        lines.append(
            f"- 毛損益 `{exp.get('gross_u')}`｜費用 `{exp.get('fees_u')}`｜"
            f"未平倉曝險名義 `{exp.get('open_notional_u')}`｜完整平倉勝率 `{exp.get('win_rate')}`｜PF `{exp.get('profit_factor')}`"
        )
    lines.append(f"- current_run_id（檔案指標，非進程證明）：`{strat.get('current_run_id')}`")
    lines.append(f"- code_fix_tag：`{strat.get('code_fix_tag')}`")
    lines.append(f"- config_hash：`{strat.get('config_hash')}`")
    lines.append(f"- runtime cwd（launch 紀錄）：`{strat.get('runtime_cwd')}`")
    lines.append(f"- 日損鎖設定：`{strat.get('daily_loss_lock_usdt')}` USDT")
    lines.append(f"- 同日涉及 run：{', '.join(strat.get('runs_touched') or []) or 'NA'}")
    lines.append(
        "- 狀態標示：本報表＝**分析工具已實作產出**；"
        "管理可靠性修復＝**設計／提案（未實作未部署）**；"
        "策略候選＝**待驗證提案（未改配置）**；排程＝**文件 only（未載入）**。"
    )
    if strat.get("cross_version_positions"):
        lines.append(
            "- 跨版本持倉：開倉版本與退出管理版本分開紀錄；"
            "不得把退出當下 runtime 當成開倉時策略身分。"
        )
        for row in strat["cross_version_positions"][:10]:
            lines.append(
                f"  - `{row['trade_id']}` 開倉 run=`{row['open_run_id']}`／退出 run=`{row['close_run_id']}`"
            )
    lines.append("")
    lines.append("## 3. A／B／C／D 績效")
    lines.append(
        "四欄必須分開閱讀：A 是帳戶水位與當日變化；B 是視窗內事件／分段；"
        "C 是跨 run 完整 trade_id 且 CLOSE 落在當日者；D 是未平倉及其已扣 OPEN 費。"
        "不可用分段勝率冒充完整交易勝率，也不可把 cash_delta 當成損益。"
    )
    lines.append("### A 帳戶")
    lines.append(
        f"- 結算界快照可用（欄位名 midnight_snapshot_ok，語意=日界±120s）："
        f"`{A.get('midnight_snapshot_ok')}`"
    )
    ss, es = A.get("start_snap") or {}, A.get("end_snap") or {}
    lines.append(
        f"- 起始快照：`{ss.get('_snap_ts','NA')}`（相對日界開始 lag={ss.get('_lag_sec','NA')}s）"
        f" equity=`{A.get('start_equity_u')}` cash=`{A.get('start_cash_u')}`"
    )
    lines.append(
        f"- 結束／最近快照：`{es.get('_snap_ts','NA')}`（相對日界結束 lag={es.get('_lag_sec','NA')}s；"
        f"部分日為負值屬預期） equity=`{A.get('end_equity_u')}` cash=`{A.get('end_cash_u')}`"
    )
    if A.get("midnight_snapshot_ok"):
        lines.append(f"- 日權益變化（結算界 20:15）：`{_fmt(A.get('day_equity_delta_u'))}`")
    else:
        lines.append(
            f"- **觀測權益變化（{_fmt_ts(ss.get('_snap_ts'))} 至 {_fmt_ts(es.get('_snap_ts'))}）**："
            f"`{_fmt(A.get('day_equity_delta_u'))}` — **不是**完整日損益；缺結算界快照。"
        )
    lines.append(f"- 日現金變化：`{_fmt(A.get('day_cash_delta_u'))}` — **cash_delta 不是 PnL**（含保證金佔用等）")
    lines.append(f"- 費用與 funding：帳戶 realized 已含紙上費用；`funding_data_gap={A.get('funding_data_gap')}`，funding 不得當已核對。")
    live = A.get("live_account_state") or {}
    lines.append(
        f"- 讀取當下 account_state（非午夜冒充）：cash_u=`{_fmt(live.get('cash_u'))}` "
        f"realized=`{_fmt(live.get('realized_u'))}` — {live.get('note')}"
    )
    dll = A.get("day_loss_lock")
    if dll is None:
        lines.append(
            "- 日損鎖狀態：**NA**（來源：報表讀取時 account_state 無明確 `day_new_entries_locked` 快照欄／"
            "或 equity 列未帶 `day_locked`；**不以**較晚讀到的 False 倒填較早報表）。設定上限見策略節。"
        )
    else:
        lines.append(f"- 日損鎖狀態：`{dll}`（來源：account_state.day_new_entries_locked 或 equity.day_locked）")
    lines.append("")
    lines.append("### B 各 run 事件／分段（非完整交易勝率）")
    lines.append(f"- 視窗內事件數：`{B.get('n_segment_events')}`")
    for rid, st in (B.get("by_run") or {}).items():
        lines.append(
            f"- run `{rid}`：OPEN={st['opens']} CLOSE={st['closes']} PARTIAL={st['partials']} "
            f"seg_net≈{st['seg_net_u']:.4f}"
        )
    lines.append(
        f"- 註：{B.get('note')}。"
        "分段淨利可含當日 OPEN 費與部分止盈，但同一 trade 可能跨 run 才完整，故 B≠C。"
    )
    lines.append("")
    lines.append("### C 完整 trade_id（當日 CLOSE）")
    lines.append(
        f"- n={Cm.get('n_complete_closed')} 勝={Cm.get('wins')} 負={Cm.get('losses')} 平={Cm.get('flats')} "
        f"勝率={Cm.get('win_rate')} 均盈虧={Cm.get('avg_net_u')} 合計={Cm.get('sum_net_u')} "
        f"PF={Cm.get('profit_factor')} 期望={Cm.get('expectancy_u')} 費用合計={Cm.get('fees_u')}"
    )
    lines.append(f"- 註：{Cm.get('note')}")
    lines.append(
        "- 完整淨利規則：若 OPEN 存在 `realized_net_u` 鍵且值非 null（**含合法 0**）則用其值；"
        "否則使用 −OPEN.fee_u；再加所有 PARTIAL／CLOSE 的 net_u。不可用 truthiness 把 0 誤判缺值；不重複扣費。"
    )
    lines.append(
        "- 當日平倉完整績效與帳戶當日損益分開；跨日 OPEN 費屬於開倉當日帳戶，"
        "不可當作今天才發生的費用事件，但仍計入該完整 trade 的 C 淨利。"
    )
    lines.append("")
    lines.append("### D 未平倉")
    lines.append(f"- 未平倉數：`{D.get('n_open')}`；當日窗口內 OPEN 費分量合計：`{D.get('open_fee_component_in_day_u')}`")
    lines.append(f"- 註：{D.get('note')}")
    for op in c["day_trades"].open_positions[:15]:
        lines.append(
            f"  - `{op['trade_id']}` {op.get('symbol')} {op.get('side')} "
            f"opened={op.get('opened_at')} fee_comp={op.get('open_fee_component_u')}"
        )
    lines.append("")
    lines.append("## 4. 管理健康")
    qb = mh.get("quote_batch_intervals") or {}
    mc = mh.get("manage_complete_intervals") or {}
    dq = mh.get("data_quality") or {}
    lines.append(
        "有效報價／管理前紀錄批次間隔，與真正管理完成間隔必須分開。"
        "前者來自 `quote_events`（寫在 manage_position 前）；後者需完成事件 schema，舊資料為 NA。"
        "觀測間隔不是設定參數，也不是市場事件時間。"
    )
    lines.append(
        f"- 有效報價／管理前批次間隔（秒）：n_batches={qb.get('n_batches')} mean={qb.get('mean')} "
        f"p50={qb.get('p50')} p95={qb.get('p95')} max={qb.get('max')}"
    )
    lines.append(f"  - {qb.get('note')}")
    lines.append(
        f"- 真正管理完成間隔：status=`{mc.get('status')}` value/mean=`{mc.get('mean', mc.get('value'))}` "
        f"reason=`{mc.get('reason', '')}`"
    )
    lines.append(
        f"- DQ 分解：timeouts≈{dq.get('timeouts')} 429≈{dq.get('http_429')} 418≈{dq.get('http_418')} "
        f"manage_blocked≈{dq.get('manage_blocked')}；counts 摘要見 metrics。"
    )
    lines.append(
        "- 超額止損：以成交 `exit_reason`／既有分析包拆解 ask 缺口與滑價假設；"
        "本節不把管理間隔尖峰直接等同市場跳空時間。"
    )
    lines.append("")
    lines.append("## 5. 策略品質與集中風險")
    lines.append(
        "以下為**條件子集診斷**與候選提案，不是完整策略回測，也不是批准開新倉。"
        "同批同向交易不可視為完全獨立樣本；費用來源為紙上模擬費率，不能冒充真實帳戶費率。"
        "進場後 1／3／5 分鐘報酬只作結果標籤，不能直接當進場可用特徵。"
        "執行可靠性修復與進場策略變更分開，避免把不同修改效果混在一起。"
    )
    for cand in cands:
        lines.append(f"### 候選 `{cand['id']}` — {cand['title']}")
        ev = cand.get("evidence") or {}
        lines.append(f"- 現有證據：`{json.dumps(ev, ensure_ascii=False)}`")
        lines.append(f"- 候選改動與理由：{cand.get('proposal')}")
        lines.append(f"- 資料缺口：{cand.get('data_gaps')}")
        lines.append(f"- 回測／回放可行性：{cand.get('offline_compare')}")
        status = "值得診斷／部署證據不足" if cand.get("worth_next_stage") else "證據不足／暫緩診斷"
        lines.append(f"- 候選狀態：**{status}**（未批准、未部署；≠可部署）")
        lines.append(f"- 與管理修復分離：{cand.get('vs_mgmt_fix')}")
    lines.append("")
    lines.append("## 6. K 線與資料品質")
    lines.append(
        "檢查當日涉及標的之 1m 是否存在於本機金庫、缺分鐘、重複、OHLC 異常。"
        "10m／15m derived 僅可用決策當下已完整收盤桶；不可使用進場後才可取得的資訊當作當時濾網。"
        "缺資料只標記；不聯網補抓、不回寫金庫。"
    )
    lines.append(f"- 缺失標的：{kq.get('missing_symbols') or '無'}")
    lines.append(f"- 網路補抓：{kq.get('network_backfill')}；金庫回寫：{kq.get('vault_writeback')}")
    shown = 0
    for sym, info in (kq.get("per_symbol") or {}).items():
        if shown >= 12:
            lines.append(f"- …其餘 {len(kq.get('per_symbol') or {}) - shown} 標的見 metrics.json／kline 細節")
            break
        lines.append(
            f"- {sym}: status={info.get('status')} bars={info.get('n_bars')} "
            f"內部缺分≈{info.get('missing_minutes_internal_est')} "
            f"頭缺≈{info.get('head_coverage_shortfall_min')} "
            f"尾缺≈{info.get('tail_coverage_shortfall_min')} "
            f"dup={info.get('duplicates_skipped')} ohlc_bad={info.get('ohlc_anomalies')}"
        )
        shown += 1
    lines.append("")
    lines.append("## 7. 最近 7 日與累計")
    hist = c.get("history_7d") or {}
    lines.append(
        f"- 可彙總日數：{hist.get('n_days_with_report')}；缺日：{hist.get('missing_days')}；"
        f"累計完整平倉 n／sum_net（僅有報表之日）：{hist.get('cum_n')}／{hist.get('cum_sum_net_u')}"
    )
    lines.append(
        "收益比較必須同時列出曝險（名義／初始風險）與市場時段差異；"
        "部分日與完整日不可直接並列當同等樣本。版本切換日應分 run／code_fix_tag 列示。"
    )
    lines.append("")
    lines.append("## 8. 下一步（最多 3）")
    for i, step in enumerate(c.get("next_steps") or [], 1):
        lines.append(f"{i}. **{step['bucket']}**：{step['text']}")
    if hyp:
        lines.append("")
        lines.append("### 假設追蹤（承接上一日，非每日重猜）")
        for h in hyp[:8]:
            lines.append(f"- [{h.get('status')}] {h.get('id')}: {h.get('text')}")
    lines.append("")
    lines.append("---")
    lines.append(
        "證據索引與 CSV 見同目錄。讀取採固定邊界；JSONL 末行不完整則延後並警告；"
        "多檔非原子快照已於 manifest 揭露。同日重跑冪等；原始帳戶／行情／策略／runtime／金庫**未修改**。"
        "本輸出不構成開新倉或變更既有開倉政策的批准。"
    )
    return "\n".join(lines) + "\n"


def _conclusions(ctx_partial: Dict[str, Any]) -> List[str]:
    Cm = ctx_partial["day_trades"].metrics_c
    A = ctx_partial["metrics_a"]
    comp = ctx_partial["completeness"]
    mc = ctx_partial["manage_health"]["manage_complete_intervals"]
    out = []
    out.append(
        f"本日標示為「{comp['status']}」：視窗內完整平倉 n={Cm.get('n_complete_closed')}，"
        f"完整淨利合計≈{Cm.get('sum_net_u')}，勝率={Cm.get('win_rate')}（僅完整 trade_id）。"
    )
    out.append(
        f"帳戶權益日變化≈{A.get('day_equity_delta_u')}（結算界快照 ok={A.get('midnight_snapshot_ok')}）；"
        f"與 C 完整平倉合計分開解讀；跨日 OPEN 費不當作今日才發生。"
    )
    out.append(
        f"管理完成間隔={mc.get('status')}（{mc.get('reason','')}）；"
        f"目前僅能報告管理前報價批次間隔，不得稱為完成態。"
    )
    cands = ctx_partial.get("candidates") or []
    worth = [c["id"] for c in cands if c.get("worth_next_stage")]
    out.append(
        f"策略調整候選中值得下一階段驗證者：{', '.join(worth) or '無（樣本或證據不足）'}；"
        f"皆為提案，非部署。"
    )
    out.append(
        "執行可靠性修復與進場條件變更分開追蹤；本輪未修改交易策略／配置／runtime／帳戶。"
    )
    return out[:5]


# 紙上實驗帳戶（與 bulk 分開；結算界／日報口徑相同）
_PAPER_EXPERIMENT_PROFILES: Dict[str, Dict[str, Any]] = {
    "paper_reverse_fixed_exit_v1": {
        "experiment_start": "2026-11-19T20:49:55+08:00",
        "exit_mode": "ATR_TP1_SL1.5_MIRROR_SAME_B",
        "label": "A 鏡像 B 同向 ATR",
        "mirror_source_profile": "paper_reverse_fixed_usdt_50_75_v1",
    },
    "paper_reverse_fixed_usdt_50_75_v1": {
        "experiment_start": "2026-11-19T23:24:47+08:00",
        "exit_mode": "FIXED_GROSS_TP50_SL75",
        "label": "B 固定毛 50/75",
    },
    "paper_reverse_fixed_usdt_50_75_inv_v1": {
        "experiment_start": "2026-11-20T00:49:04+08:00",
        "exit_mode": "FIXED_GROSS_TP50_SL75_MIRROR_INV_B",
        "label": "C 鏡像 B 反向",
        "mirror_source_profile": "paper_reverse_fixed_usdt_50_75_v1",
    },
}


def _reverse_experiment_meta(
    *,
    paths: SetupPaths,
    acct: Dict[str, Any],
    fills: List[Dict[str, Any]],
    day_trades: Any,
    start: datetime,
    end: datetime,
    asof: datetime,
    hashes: Dict[str, Any],
    launch: Dict[str, Any],
) -> Dict[str, Any]:
    """紙上實驗帳戶專用口徑（A/B/C 相同結算／報告邏輯）；其他 profile 回傳空 dict。"""
    prof_meta = _PAPER_EXPERIMENT_PROFILES.get(paths.profile_id)
    if not prof_meta:
        return {}

    experiment_start = parse_iso(str(prof_meta["experiment_start"]))
    coverage_start = start
    if experiment_start and experiment_start > start:
        coverage_start = experiment_start
    coverage_end = end if asof >= end else asof

    # config snapshot from current/latest run
    cfg: Dict[str, Any] = {}
    run_dir = paths.current_run_dir
    if run_dir and (run_dir / "config_snapshot.json").exists():
        cfg = _load_json(run_dir / "config_snapshot.json") or {}
    cutoff_iso = cfg.get("NEW_ENTRIES_UNTIL_ISO") or "2026-11-20T20:15:00+08:00"
    cut = parse_iso(str(cutoff_iso))
    if cut is None:
        cutoff_status = "未能解析 NEW_ENTRIES_UNTIL_ISO"
    elif asof < cut:
        cutoff_status = f"已寫入設定／程式閘門；尚未到點（截止 {cutoff_iso}）"
    else:
        cutoff_status = f"截止已到（{cutoff_iso}）；應阻擋新開倉並續管持倉（依運行中程式 OBSERVATION_CUTOFF）"

    opens = [f for f in fills if f.get("event") == "OPEN"]
    long_to_short = [
        f
        for f in opens
        if f.get("direction_flip") is True and f.get("orig_side") == "LONG" and f.get("side") == "SHORT"
    ]
    short_to_long = [
        f
        for f in opens
        if f.get("direction_flip") is True and f.get("orig_side") == "SHORT" and f.get("side") == "LONG"
    ]
    # also accept snapshot-proven pairs via fill fields only
    l2s_status = (
        f"有成交證據 n={len(long_to_short)}"
        if long_to_short
        else "尚無成交證據"
    )
    s2l_status = (
        f"有成交證據 n={len(short_to_long)}"
        if short_to_long
        else "尚無成交證據"
    )
    mirror_n = sum(
        1
        for f in opens
        if "MIRROR_INVERSE" in (f.get("source_tags") or [])
        or f.get("mirrored_from_trade_id")
    )
    mirror_status = f"有鏡像進場 n={mirror_n}" if mirror_n else "尚無鏡像進場"

    positions = acct.get("positions") or {}
    margin_used = sum(float(p.get("margin_u") or 0) for p in positions.values())
    open_notional = sum(float(p.get("notional_initial") or p.get("notional_u") or 0) for p in positions.values())
    # floating from last equity snap if available
    unreal = None
    equity = None
    eq_rows = read_jsonl_bounded(paths.root_equity).rows
    for rd in list_run_dirs(paths):
        eq_rows.extend(read_jsonl_bounded(rd / "equity_snapshots.jsonl").rows)
    # pick latest by recorded_at/ts
    latest = None
    for row in eq_rows:
        ts = parse_iso(row.get("recorded_at") or row.get("ts") or row.get("time"))
        if ts is None:
            continue
        if latest is None or ts > latest[0]:
            latest = (ts, row)
    if latest:
        unreal = latest[1].get("floating_u")
        if unreal is None:
            unreal = latest[1].get("unrealized_u")
        equity = latest[1].get("equity") or latest[1].get("equity_u")

    initial = float(acct.get("initial_capital_u") or cfg.get("INITIAL_CAPITAL_USDT") or 50000.0)
    realized = float(acct.get("realized_net_u") or 0.0)
    fees = float(acct.get("realized_fees_u") or 0.0)
    cash = float(acct.get("cash_u") or 0.0)
    if equity is None:
        equity = initial + realized + float(unreal or 0.0)
    # gross ≈ realized + fees (fees already in realized_net as negative); keep explicit
    gross = realized + fees

    Cm = getattr(day_trades, "metrics_c", {}) or {}
    cal_windows = [
        {
            "calendar_day": "2026-11-19",
            "note": "啟動後至當日午夜；日損上限 8000 適用該日曆日",
        },
        {
            "calendar_day": "2026-11-20",
            "note": "午夜至報告結算 20:15；另計新的 8000 日損上限（≠整場累計保證）",
        },
    ]

    return {
        "profile_id": paths.profile_id,
        "experiment_label": prof_meta.get("label"),
        "exit_mode": prof_meta.get("exit_mode"),
        "mirror_source_profile": prof_meta.get("mirror_source_profile"),
        "mirror_entry_status": mirror_status,
        "experiment_start": experiment_start.isoformat() if experiment_start else None,
        "report_window_start": start.isoformat(),
        "report_window_end": end.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "coverage_note": "非完整24小時；自穩定啟動起算至 asof／日界",
        "settlement_boundary": "20:15 Asia/Taipei（報告日界；與引擎日曆午夜日損切日分離）",
        "report_schedule": "20:25 Asia/Taipei",
        "initial_capital_u": initial,
        "daily_loss_lock_usdt": float(
            cfg.get("DAILY_LOSS_LOCK_USDT")
            or launch.get("daily_loss_lock_usdt")
            or 8000.0
        ),
        "calendar_day_loss_windows": cal_windows,
        "new_entries_until_iso": cutoff_iso,
        "observation_cutoff_status": cutoff_status,
        "long_to_short_fill_status": l2s_status,
        "short_to_long_fill_status": s2l_status,
        "realized_net_u": realized,
        "unrealized_u": unreal,
        "equity_u": equity,
        "cash_u": cash,
        "margin_used_u": margin_used,
        "open_notional_u": open_notional,
        "fees_u": fees,
        "gross_u": gross,
        "win_rate": Cm.get("win_rate"),
        "profit_factor": Cm.get("profit_factor"),
        "config_hash": hashes.get("config_hash"),
        "manage_note": "保證金占用≠虧損；本金注入≠盈利；新舊 profile 報告不得混算",
        "fixed_gross_tp_usdt": cfg.get("FIXED_GROSS_TP_USDT"),
        "fixed_gross_sl_usdt": cfg.get("FIXED_GROSS_SL_USDT"),
        "fixed_gross_enabled": bool(cfg.get("FIXED_GROSS_EXIT_ENABLED")),
        "mirror_only_entries": bool(cfg.get("MIRROR_ONLY_ENTRIES")),
    }


def generate_day_report(
    setup_root: Path,
    day: date,
    *,
    asof: Optional[datetime] = None,
    cutoff_hour: int = 20,
    cutoff_minute: int = 25,
    profile_id: str = "paper_bulk_test_v1",
) -> Dict[str, Any]:
    paths = discover_setup(setup_root, profile_id=profile_id)
    start, end = day_window(day, TAIPEI)
    asof = asof or datetime.now(TAIPEI)
    out_dir = paths.daily_reports / day.isoformat()
    lock_path = paths.daily_reports / ".locks" / f"{day.isoformat()}.lock"

    with FileLock(lock_path):
        fills, fills_meta = load_all_fills(paths)
        by_tid = group_by_trade_id(fills)
        day_trades = analyze_day_trades(fills, start, end)

        eq = read_jsonl_bounded(paths.root_equity).rows
        # also merge run equity
        for rd in list_run_dirs(paths):
            eq.extend(read_jsonl_bounded(rd / "equity_snapshots.jsonl").rows)
        acct = _load_json(paths.account_state) or {}
        # normalize live fields
        metrics_a = account_day_metrics(eq, start, end, acct)

        run_dirs = list_run_dirs(paths)
        manage_health = analyze_manage_health(run_dirs, start, end)

        symbols = sorted(
            {
                *(t.get("symbol") for t in day_trades.complete_closed_today if t.get("symbol")),
                *(o.get("symbol") for o in day_trades.open_positions if o.get("symbol")),
            }
        )
        kline_qa = analyze_kline_qa(paths.vault_dir, symbols, start, end)
        candidates = build_strategy_candidates(day_trades, by_tid)

        first_fill = fills[0].get("recorded_at") if fills else None
        last_fill = fills[-1].get("recorded_at") if fills else None
        completeness = _completeness(start, end, asof, fills_meta, first_fill, last_fill)

        launch = paths.launch or {}
        hashes = paths.code_hashes or {}
        runs_touched = sorted(
            {
                *(f.get("run_id") for f in day_trades.segment_events if f.get("run_id")),
                *(t.get("open_run_id") for t in day_trades.complete_closed_today if t.get("open_run_id")),
                *(t.get("close_run_id") for t in day_trades.complete_closed_today if t.get("close_run_id")),
            }
        )
        cross = [t for t in day_trades.complete_closed_today if t.get("cross_version")]
        experiment_meta = _reverse_experiment_meta(
            paths=paths,
            acct=acct,
            fills=fills,
            day_trades=day_trades,
            start=start,
            end=end,
            asof=asof,
            hashes=hashes,
            launch=launch,
        )
        strategy_identity = {
            "profile_id": launch.get("profile_id") or acct.get("profile_id") or paths.profile_id,
            "account_root": str(paths.account_root),
            "daily_reports_root": str(paths.daily_reports),
            "current_run_id": paths.current_run_id,
            "code_fix_tag": launch.get("code_fix_tag") or hashes.get("code_fix_tag"),
            "config_hash": hashes.get("config_hash"),
            "runtime_cwd": launch.get("cwd"),
            "daily_loss_lock_usdt": launch.get("daily_loss_lock_usdt"),
            "runs_touched": runs_touched,
            "cross_version_positions": [
                {
                    "trade_id": t["trade_id"],
                    "open_run_id": t["open_run_id"],
                    "close_run_id": t["close_run_id"],
                }
                for t in cross
            ],
            "proposal_vs_deploy": {
                "daily_report_tool": "implemented_offline",
                "mgmt_reliability_fix": "design_only",
                "strategy_candidates": "proposal_only",
                "schedule": "documented_not_loaded",
            },
        }

        # history from prior manifests
        hist = _history_7d(paths.daily_reports, day)
        hyp_path = paths.daily_reports / "STRATEGY_HYPOTHESES.md"
        hypotheses = _parse_hypotheses(hyp_path)

        next_steps = [
            {
                "bucket": "已證實問題",
                "text": "管理完成事件缺失 → 完成間隔 NA；bookTicker 逾時導致管理前間隔拉長（見既有分析包）。修復實作仍待批准。",
            },
            {
                "bucket": "待驗證策略假設",
                "text": "；".join(
                    f"{c['id']}:{c['title']}" for c in candidates if c.get("worth_next_stage")
                )
                or "樣本不足，暫不升級假設",
            },
            {
                "bucket": "待批准修改",
                "text": "管理可靠性實作／部署；策略候選進樣外驗證；launchd 載入。皆未在本輪執行。",
            },
        ]

        ctx: Dict[str, Any] = {
            "day": day.isoformat(),
            "completeness": completeness,
            "metrics_a": metrics_a,
            "day_trades": day_trades,
            "manage_health": manage_health,
            "kline_qa": kline_qa,
            "candidates": candidates,
            "strategy_identity": strategy_identity,
            "experiment_meta": experiment_meta,
            "history_7d": hist,
            "hypotheses_track": hypotheses,
            "next_steps": next_steps,
            "fills_meta": fills_meta,
            "inventory": inventory_fields(),
        }
        ctx["conclusions"] = _conclusions(ctx)
        summary_md = build_daily_summary_md(ctx)
        zh_n = _zh_char_count(summary_md)
        # pad lightly if short (keep substance, avoid spam)
        if zh_n < 2000:
            summary_md += (
                "\n## 附記（口徑與限制）\n"
                "本工具為唯讀離線復盤：固定每次讀取邊界，JSONL 末行不完整則延後並警告；"
                "多檔快照非原子，已於 manifest 揭露。完整交易淨利不可用 truthiness 把合法 0 當缺值。"
                "當日平倉完整績效與帳戶當日損益分開；未平倉 OPEN 費留在 A／D，不混入 C 勝率。"
                "管理前報價間隔 ≠ 設定參數 ≠ 市場事件時間。K 線缺漏只標記，不聯網、不回寫金庫。"
                "策略候選僅條件子集診斷，跳過交易會改變資金與後續機會，故非完整策略回測。"
                "不得用 1m OHLC 捏造精確 book 成交路徑。排程檔已提供但未安裝載入。"
                "PID／固定 run 路徑不能單獨認定當前運行狀態；本報表不依賴進程存活證明。\n"
            )
            zh_n = _zh_char_count(summary_md)

        out_dir.mkdir(parents=True, exist_ok=True)
        # trades csv
        trades_csv = out_dir / "trades_complete.csv"
        with trades_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "trade_id",
                    "symbol",
                    "side",
                    "opened_at",
                    "closed_at",
                    "open_run_id",
                    "close_run_id",
                    "complete_net_u",
                    "exit_reason",
                    "fees_u",
                    "cross_version",
                ],
            )
            w.writeheader()
            for t in day_trades.complete_closed_today:
                w.writerow({k: t.get(k) for k in w.fieldnames})

        evidence = []
        evidence.append({"kind": "fills", "path": str(paths.root_fills), "meta": fills_meta})
        evidence.append({"kind": "equity", "path": str(paths.root_equity)})
        evidence.append({"kind": "account_state", "path": str(paths.account_state), "note": "live; not midnight"})
        for rd in run_dirs:
            for name in ("quote_events.jsonl", "data_quality.jsonl", "launch.json"):
                p = rd / name
                if p.exists():
                    evidence.append({"kind": name, "path": str(p), "run": rd.name})

        evid_csv = out_dir / "evidence_index.csv"
        with evid_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["kind", "path", "run", "note"])
            w.writeheader()
            for e in evidence:
                w.writerow(
                    {
                        "kind": e.get("kind"),
                        "path": e.get("path"),
                        "run": e.get("run", ""),
                        "note": e.get("note", ""),
                    }
                )

        metrics = {
            "day": day.isoformat(),
            "completeness": completeness,
            "A": {k: v for k, v in metrics_a.items() if k not in ("start_snap", "end_snap", "live_account_state")}
            | {
                "start_snap_ts": (metrics_a.get("start_snap") or {}).get("_snap_ts"),
                "start_lag_sec": (metrics_a.get("start_snap") or {}).get("_lag_sec"),
                "end_snap_ts": (metrics_a.get("end_snap") or {}).get("_snap_ts"),
                "end_lag_sec": (metrics_a.get("end_snap") or {}).get("_lag_sec"),
            },
            "B": day_trades.metrics_b,
            "C": day_trades.metrics_c,
            "D": day_trades.metrics_d,
            "manage_health": manage_health,
            "kline_qa": {
                "missing_symbols": kline_qa.get("missing_symbols"),
                "n_symbols": len(kline_qa.get("per_symbol") or {}),
            },
            "candidates": [
                {
                    "id": x["id"],
                    "worth_next_stage": x.get("worth_next_stage"),
                    "insufficient": x.get("insufficient"),
                    "evidence": x.get("evidence"),
                }
                for x in candidates
            ],
            "zh_char_count_summary": zh_n,
            "tool_version": __version__,
            "schema": SCHEMA_VERSION,
            "profile_id": paths.profile_id,
            "experiment_meta": experiment_meta,
        }
        manifest = {
            "tool_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "day": day.isoformat(),
            "timezone": "Asia/Taipei",
            "profile_id": paths.profile_id,
            "account_root": str(paths.account_root),
            "daily_reports_root": str(paths.daily_reports),
            "window": completeness["window"],
            "asof": completeness["asof"],
            "completeness": completeness,
            "experiment_meta": experiment_meta,
            "sources": fills_meta,
            "non_atomic_multi_file": True,
            "outputs": [
                "DAILY_SUMMARY.md",
                "metrics.json",
                "trades_complete.csv",
                "evidence_index.csv",
                "manifest.json",
            ],
            "quality": {
                "truncated_tails": fills_meta.get("truncated_tails"),
                "midnight_snapshot_ok": metrics_a.get("midnight_snapshot_ok"),
                "manage_complete_events": manage_health["manage_complete_intervals"].get("status"),
            },
            "mutations": {
                "account": False,
                "vault": False,
                "strategy": False,
                "runtime": False,
                "schedule_loaded": False,
            },
            "inventory": inventory_fields(),
        }

        atomic_write_text(out_dir / "DAILY_SUMMARY.md", summary_md)
        atomic_write_json(out_dir / "metrics.json", metrics)
        atomic_write_json(out_dir / "manifest.json", manifest)

        _update_index(paths.daily_reports, day, completeness["status"], Cm_sum=day_trades.metrics_c.get("sum_net_u"))
        _append_changelog(paths.daily_reports, day, completeness["status"])
        _ensure_hypotheses(paths.daily_reports, candidates)

        return {
            "out_dir": str(out_dir),
            "completeness": completeness,
            "zh_chars": zh_n,
            "metrics_c": day_trades.metrics_c,
            "candidates": candidates,
            "summary_path": str(out_dir / "DAILY_SUMMARY.md"),
        }


def _history_7d(daily_root: Path, day: date) -> Dict[str, Any]:
    from datetime import timedelta

    days = []
    missing = []
    cum_n = 0
    cum_sum = 0.0
    for i in range(7):
        d = day - timedelta(days=i)
        mpath = daily_root / d.isoformat() / "metrics.json"
        if not mpath.exists():
            if d != day:
                missing.append(d.isoformat())
            continue
        try:
            m = json.loads(mpath.read_text())
            C = m.get("C") or {}
            days.append(d.isoformat())
            cum_n += int(C.get("n_complete_closed") or 0)
            cum_sum += float(C.get("sum_net_u") or 0)
        except Exception:
            missing.append(d.isoformat())
    return {
        "n_days_with_report": len(days),
        "days": days,
        "missing_days": missing,
        "cum_n": cum_n,
        "cum_sum_net_u": cum_sum,
    }


def _parse_hypotheses(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- [") and "]" in line:
            status = line[line.find("[") + 1 : line.find("]")]
            rest = line[line.find("]") + 1 :].strip()
            hid = rest.split(":", 1)[0].strip() if ":" in rest else rest[:40]
            out.append({"id": hid, "status": status, "text": rest})
    return out


def _ensure_hypotheses(daily_root: Path, candidates: List[Dict[str, Any]]) -> None:
    path = daily_root / "STRATEGY_HYPOTHESES.md"
    if not path.exists():
        lines = [
            "# 策略假設追蹤（跨日）",
            "",
            "狀態：open / supporting / weak / rejected / pending_approval",
            "規則：承接上一日；不每天重頭猜；執行可靠性 vs 進場策略分開。",
            "",
        ]
        for c in candidates:
            st = "open" if c.get("worth_next_stage") else "weak"
            lines.append(f"- [{st}] {c['id']}: {c['title']} — 提案 only，未部署")
        lines.append("")
        atomic_write_text(path, "\n".join(lines))
        return
    # append missing ids
    text = path.read_text(encoding="utf-8")
    extra = []
    for c in candidates:
        if c["id"] not in text:
            st = "open" if c.get("worth_next_stage") else "weak"
            extra.append(f"- [{st}] {c['id']}: {c['title']} — 提案 only，未部署")
    if extra:
        atomic_write_text(path, text.rstrip() + "\n" + "\n".join(extra) + "\n")


def _update_index(daily_root: Path, day: date, status: str, *, Cm_sum: Any) -> None:
    path = daily_root / "DAILY_INDEX.md"
    line = f"| {day.isoformat()} | {status} | C_sum_net≈{Cm_sum} | `{day.isoformat()}/DAILY_SUMMARY.md` |"
    if not path.exists():
        content = (
            "# 每日復盤索引\n\n"
            "| 日期 | 完整性 | C合計 | 路徑 |\n|---|---|---|---|\n"
            f"{line}\n"
        )
        atomic_write_text(path, content)
        return
    text = path.read_text(encoding="utf-8")
    # idempotent replace same day row
    rows = []
    found = False
    for ln in text.splitlines():
        if ln.startswith(f"| {day.isoformat()} |"):
            rows.append(line)
            found = True
        else:
            rows.append(ln)
    if not found:
        if rows and rows[-1].strip() == "":
            rows.append(line)
        else:
            rows.append(line)
    atomic_write_text(path, "\n".join(rows) + "\n")


def _append_changelog(daily_root: Path, day: date, status: str) -> None:
    path = daily_root / "CHANGELOG.md"
    ts = datetime.now(TAIPEI).isoformat()
    entry = f"- {ts} 產製／重跑 `{day.isoformat()}`（{status}）tool={__version__} schema={SCHEMA_VERSION}\n"
    if not path.exists():
        atomic_write_text(path, "# 每日復盤 CHANGELOG\n\n" + entry)
        return
    text = path.read_text(encoding="utf-8")
    atomic_write_text(path, text.rstrip() + "\n" + entry)
