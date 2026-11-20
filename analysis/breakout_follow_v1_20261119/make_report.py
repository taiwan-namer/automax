"""Render FULL_OVERNIGHT_ANALYSIS.md from out/*.json (no new market access, no engine import)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE / "out"
PACK_REL = "output/breakout_follow_v1/exports/20261119T024357_183e09f4_overnight_pack_20261119T092335"
PACK = REPO / PACK_REL
TZ = timezone(timedelta(hours=8))

R = json.loads((OUT / "results.json").read_text())
AV = json.loads((OUT / "atr_volx_ratio.json").read_text())
TEST_TXT = (OUT / "test_output.txt").read_text()
PROBE_TXT = (OUT / "defect_probe_output.txt").read_text()


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def f(x, d=2):
    if x is None:
        return "—"
    if isinstance(x, (int,)) and not isinstance(x, bool):
        return f"{x:,}"
    try:
        return f"{x:,.{d}f}"
    except (TypeError, ValueError):
        return str(x)


def px(x):
    if x is None:
        return "—"
    return f"{x:.10g}"


def hhmmss(s):
    if not s:
        return "—"
    return s[11:19]


def ms_iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=TZ).isoformat(timespec="seconds")


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


L: list[str] = []
w = L.append

acc = R["accounting"]
dd = R["drawdown"]
start = acc["start_snapshot"]
end = acc["end_snapshot"]
trades = R["trades"]
new_tr = [t for t in trades if t["source"] == "THIS_RUN"]
cross_tr = [t for t in trades if t["source"] != "THIS_RUN"]
kq = R["kline_quality"]
ps = R["kline_partial_stats"]
counts = R["counts"]
sim = R["sim"]

realized_inc = acc["acct_realized"] - start["realized_net_u"]
new_life = sum(t["lifecycle_net_total"] for t in new_tr)
cross_real_in_run = sum(sum(e["net"] for e in t["exits"]) for t in cross_tr)
cross_contrib = cross_real_in_run - start["floating_u"]
win_change = dd["window_change"]
fees_run = acc["acct_fees"] - start["realized_fees_u"]
gross_sum = acc["gross_all_time"]

test_total = ps["test"][0] + ps["test"][1]
warm_total = ps["warmup"][0] + ps["warmup"][1]

fixed_delta = {}
for t in trades:
    a, b = t["exit_replay"], t["exit_replay_extreme_fixed"]
    if a and b:
        fixed_delta[t["symbol"]] = b["gross"] - a["gross"]
fixed_total = sum(fixed_delta.values())

posthoc_ok = {}
for t in trades:
    p = t.get("posthoc_signal_bar") or {}
    posthoc_ok[t["symbol"]] = bool(p.get("side") == t["side"] and (p.get("vol_x") or 0) >= 3.0)
n_invalid = sum(1 for v in posthoc_ok.values() if not v)

# ------------------------------------------------------------------ header
now = datetime.now(TZ).isoformat(timespec="seconds")
w("# breakout_follow_v1 隔夜 run 完整唯讀稽核與策略研究報告")
w("")
w(f"- run_id：`20261119T024357_183e09f4`｜config_hash：`89cf2579037ba8ce`｜git_commit：`UNKNOWN`（非 git repo）")
w("- 時區：Asia/Taipei (UTC+8)。本報告所有時間皆為 +08:00。")
w("- 主資料源：固定匯出快照 `" + PACK_REL + "/`，截止 `2026-11-19T09:23:35.913096+08:00`。")
w("- 額外來源（不同截止時間，逐項標明）：pre_overnight 凍結檔（02:43:56）、根目錄 live `fills.jsonl`（分析時讀取）、事後公開 REST K 線（POSTHOC_EXTERNAL_REFERENCE，**非當時輸入**）。")
w(f"- 報告產生：{now}。全程唯讀：未改策略／參數／帳戶／歷史／日損鎖，未重啟或停止交易，未呼叫任何私有 API，未讀取 .env／金鑰。")
w("- 標記：【直接紀錄】原始檔案欄位；【重算確認】本報告以獨立腳本重算且與紀錄一致（或明確給出差異）；【推估】依假設之反事實或近似；【無法判定】資料不足。")
w("")

# ------------------------------------------------------------------ 1
w("## 1. 一頁結論")
w("")
w(f"1. **行情資料缺陷（P0）：live 引擎用的 1m K 大量是「未收盤快照」。** `fetch_klines_incremental` 以 `concat([cached, fresh]).drop_duplicates('open_time')`（預設 keep='first'）合併，舊快取版本永遠勝出；某根 K 第一次被抓到時若尚未收盤，之後就不會被收盤版覆蓋。測試期 {f(test_total)} 根封存 K 中 {f(ps['test'][1])} 根（{ps['test'][1]/test_total:.1%}）與事後交易所收盤 K 不同；全部測試段 K 中有 32.0% 的成交量不到真值 5%。【重算確認】決策時 ATR 中位數只有真值的 {AV['atr_ratio_live_over_posthoc']['50']:.2f} 倍（{AV['share_atr_live_lower']:.1%} 的決策列 ATR 偏低），vol_x 比值 P10–P90 為 {AV['volx_ratio_live_over_posthoc']['10']:.3f}–{AV['volx_ratio_live_over_posthoc']['90']:.2f}，訊號基本是雜訊。【重算確認】7 筆進場中 {n_invalid} 筆在完整 K 上不成立（PEPE、WLD、HUMA、QNT）。【重算確認，事後參考資料】")
w(f"2. **持倉管理缺陷（P0）：移動停利的極值從未被保存。** `portfolio.manage_position` 第 379–384 行更新 `pos.extreme_price` 後，第 418 行 `pos = get_position(...)` 重新讀取未保存的舊值，所以「移動停利」實際是 `進場價 ∓ 1.5×最新ATR`，只會因 ATR 縮小而移動，永遠不跟隨有利價格。22/22 筆停損更新與 7/7 筆出場時間價格均被「極值凍結」重播逐筆重現，離線探針亦確認。【重算確認】因此 5 筆 TRAILING_STOP 出場全部虧損。以同一批實錄報價、只修正極值保存，PEPE/WLD/HUMA/QNT 毛損益合計可改善約 {fixed_total:+.2f} U。【推估】")
w(f"3. **帳務（P1）：** 現金 `cash_u` 從未計入 gross，只扣費用與保證金，所以截止時 cash−equity = {acc['cash_minus_equity_end']:.4f} = −Σgross（{gross_sum:+.4f}）。【重算確認】本 run 期間現金最低 {f(acc['min_cash_u_during_run'])}、修正後可用資金（equity−used_margin）最低 {f(acc['min_corrected_cash_during_run'])}，都遠高於單筆保證金 1,000，且 INSUFFICIENT_MARGIN 為 0 列，**本 run 未因此改變任何開倉**。【重算確認】這是潛在風險，不是本 run 的實際損害。")
w(f"4. **損益拆分：** 帳戶自建立累計已實現 {acc['acct_realized']:+.4f} U；本 run 已實現增量 {realized_inc:+.4f}；本 run 起訖權益變化 {win_change:+.4f}（起點 {start['equity']:.4f} 含跨 run 浮動 {start['floating_u']:+.4f}）＝ 本 run 新開 5 筆生命週期 {new_life:+.4f} ＋ 跨 run 2 筆在本 run 的貢獻 {cross_contrib:+.4f}。【重算確認】資金費率全為 0 且 funding_data_gap=True，**資金費未計入**。【直接紀錄】")
w(f"5. **日損鎖：** 日起始權益固定在帳戶建立時的 10,000（02:32 建立、Asia/Taipei 日界），本 run 起點只剩 {start['equity']-9700:.2f} U 空間；03:02:03 權益觸鎖，之後 6 小時 0 開倉，265 個通過全部訊號條件的候選被擋。【直接紀錄】被擋候選的事後 5 分鐘平均淨報酬為 −18.7 bps（機會研究，非可實現收益）。【推估】")
w("6. **部分停利／保本／反手：** 程式有分支、單元測試在隔離目錄通過，但**本 run 實際觸發 0 次**。最高逐報價淨浮盈只有 +41.19 U（QNT），未達 100 U；持倉期間 0 列反向突破，反手條件從未成立。【重算確認】之前稽核引用的 PARTIAL_TP/REVERSE 成交是單元測試污染列（`t:AAAUSDT`、`t:BBBUSDT`，02:31:40 寫入根目錄 fills），**不是 run 證據**，此處更正。【直接紀錄】")
w("7. **基準重播已完成：** 訊號指標 27,063/27,063 列完全一致、進場檢查 814/814 一致、停損更新 22/22 一致、出場 7/7 一致；以封存 K 跑「照程式」模擬，重現相同 5 筆開倉與 03:02 觸鎖。【重算確認】敏感度測試全是單夜樣本內結果、各變體權益都低於 10,000，**不足以支持任何參數調整**；必須先修 P0 再累積多夜資料。【推估】")
w("")

# ------------------------------------------------------------------ 2
w("## 2. 資料盤點與限制")
w("")
w("### 2.1 檔案清單（主快照與額外來源）")
w("")
rows = []
for x in R["inventory"]:
    if "klines_1m/" in x["path"]:
        continue
    tr = x.get("time_range")
    rows.append([x["source"], f"`{x['path']}`", f(x["bytes"]), (x.get("sha256") or "")[:16], f(x.get("records")) if x.get("records") is not None else "—",
                 f"{hhmmss(tr[0])}–{hhmmss(tr[1])}" if tr else "—"])
w(table(["來源", "路徑", "bytes", "sha256[:16]", "筆數", "時間範圍"], rows))
w("")
w(f"- 本報告實際讀取檔案數：**{counts['files_read']:,}**（含 27,063 個 decision_snapshots、72 檔封存 K 線、72 檔事後 K 線、交易／權益／報價／宇宙／設定檔與額外來源）。【直接紀錄】")
w(f"- 封存 K 線：{counts['kline_symbols']} 標的、{counts['kline_bars']:,} 根；事後 K 線：{counts['posthoc_symbols']} 標的、{counts['posthoc_bars']:,} 根；決策列 {counts['decision_rows']:,}；報價事件 {counts['quote_rows']:,}；權益快照 {counts['equity_rows']}；本 run fills {counts['fills']}。【直接紀錄】")
w("- decision_snapshots 以「檔名排序後逐檔 sha256 串接」計組合雜湊（見 results.json `inventory`）。")
w("- 根目錄 live `fills.jsonl` 仍在附加（截止時間不同）；它有 20 列，含 4 列測試污染（`t:AAAUSDT` OPEN/CLOSE_PARTIAL PARTIAL_TP +102.21、`t:BBBUSDT` OPEN/CLOSE REVERSE_SIGNAL −7.00，均 02:31:40）。這 4 列**沒有**進入帳戶 realized：−72.3555 = 龙虾 開倉費 −5.0 ＋ 平倉 −57.3555 ＋ PEPE/CROSS 開倉費 −10.0。【重算確認】")
w("")
w("### 2.2 程式版本")
w("")
rows = [[x["file"].split("/")[-1], "一致" if x["match"] else "**不一致**"] for x in R["code_hash_compare"]]
w(table(["檔案", "目前檔案 sha256 vs run 啟動時 code_hashes.json"], rows))
w("")
w("- engine.py（02:45 修改）、market_data.py（02:44）、run_store.py（02:46）在 run 啟動（02:43:57）後被修改，常駐 PID 86206 載入的是 02:43 版本，**這三檔版本不確定**。【直接紀錄】已知修改內容是暖機回補函式／啟動呼叫與 data_quality 格式。【推估】")
w("- 不過行為已實證：訊號（signals.py，雜湊一致）與停損管理（portfolio.py，雜湊一致）的重播 100% 吻合。K 線合併的 keep-first 行為由封存資料的雙峰分布佐證（2.4 節）。【重算確認】")
w("")
w("### 2.3 K 線品質（封存 = 引擎當時所見）")
w("")
w(f"- 重複 open_time：{sum(x['dups'] for x in kq)}；順序錯誤：{sum(x['order_violations'] for x in kq)}。【重算確認】")
gaps = [x for x in kq if x["gap_count"]]
w("- 缺口：" + "；".join(f"{x['symbol']} {x['gap_count']} 段共 {x['gap_minutes']} 分（{', '.join(g[0][11:16]+'–'+g[1][11:16] for g in x['gaps'])}）" for x in gaps) + "。【重算確認】XPL 的 36 分缺口與宇宙進出相符（不在宇宙時不抓 K）。【推估】")
w(f"- 截止時未收盤的最後一根：{sum(x['unclosed_at_cutoff'] for x in kq)} 個標的（09:22/09:23 那根），但封存檔的 `is_closed` 欄**全部為 True**（`run_store.py` 第 252 行無條件設 True），屬於標記錯誤。【重算確認】")
lt100 = [x for x in kq if x["warmup_bars_before_start"] < 100]
w(f"- 啟動前暖機 K ≥100：{len(kq)-len(lt100)}/{len(kq)}；不足者 " + "、".join(f"{x['symbol']}（首根 {x['first'][11:16]}，啟動後才進宇宙）" for x in lt100) + "。【直接紀錄】")
w("")
w("### 2.4 封存 K vs 事後交易所收盤 K（POSTHOC_EXTERNAL_REFERENCE）")
w("")
w(table(["區段", "完全一致", "不一致", "不一致比例"], [
    ["暖機段（啟動前）", f(ps["warmup"][0]), f(ps["warmup"][1]), f"{ps['warmup'][1]/warm_total:.1%}"],
    ["測試段（02:42 起）", f(ps["test"][0]), f(ps["test"][1]), f"{ps['test'][1]/test_total:.1%}"],
]))
w("")
w("測試段封存成交量 ÷ 真值成交量的分布（n=27,242）：【重算確認】")
w("")
w(table(["比值區間", "<5%", "5–25%", "25–50%", "50–90%", "90–<100%", "=100%", ">100%"],
        [["占比", "32.0%", "12.5%", "1.8%", "1.4%", "10.9%", "41.4%", "0.0%"]]))
w("")
w("- 收盤價不同：51.5%；封存高低幅/真值高低幅 P10 = 0（至少 10% 的 K 在封存中 high==low，真值卻有波動）。【重算確認】")
w("- 例：BTCUSDT 02:52 封存量 3.202、真值 101.613；02:53 封存 0.147、真值 123.447。【直接紀錄＋事後參考】")
w(f"- 對每列決策用相同公式比較：ATR(live)/ATR(真值) P10/P50/P90 = {AV['atr_ratio_live_over_posthoc']['10']:.3f}/{AV['atr_ratio_live_over_posthoc']['50']:.3f}/{AV['atr_ratio_live_over_posthoc']['90']:.3f}；vol_x 比值 P10/P25/P50/P75/P90 = " + "/".join(f"{AV['volx_ratio_live_over_posthoc'][k]:.3f}" for k in ("10", "25", "50", "75", "90")) + f"；訊號根前 20 根中平均 {AV['prior20_bars_differing_mean']:.1f} 根與真值不同。【重算確認】")
w(f"- vol_x 門檻判定交叉表（live vs 真值）：{json.dumps(R['volx_live_vs_posthoc'], ensure_ascii=False)}；突破方向交叉表：{json.dumps(R['side_live_vs_posthoc'], ensure_ascii=False)}。live 判定 vol_x 通過 2,408 列中，真值也通過的只有 608 列。【重算確認】")
w("- 有些 K 完整、有些只是幾秒快照，原因可能是某分鐘第一次抓取時該 K 尚未在回應中出現，或出現時已收盤。**無法從現有紀錄判定**。【無法判定】")
w("")
w("### 2.5 宇宙／top10 覆蓋與掃描對齊")
w("")
u = R["universe"]
sc = R["scan_coverage"]
w(f"- 宇宙快照 {u['snapshots']} 次（{hhmmss(u['first'])}–{hhmmss(u['last'])}），刷新間隔 {u['refresh_gaps_min']} 分；每次合格 {min(u['eligible_counts'])}–{max(u['eligible_counts'])} 檔；聯集 {u['union_size']} 檔，與封存 K 標的完全一致。排名依據：{u['rank_basis']}；最低 24h 成交額 {f(u['min_quote_volume'],0)}。常駐成員 {u['always_members']} 檔；曾進漲幅前十 {u['top_gainers_ever']} 檔、跌幅前十 {u['top_losers_ever']} 檔。【直接紀錄】")
w(f"- 掃描：訊號根 {sc['distinct_signal_bars']} 分（{hhmmss(sc['first_S'])}–{hhmmss(sc['last_S'])}），應有 {sc['expected_minutes']}，缺 {len(sc['missing_minutes'])}；每次掃描 {sc['symbols_per_scan_min']}–{sc['symbols_per_scan_max']} 檔；有 {sc['scans_missing_eligible']} 次掃描漏掉某個合格標的（例：{', '.join(e[0][11:16]+' '+e[1][0] for e in sc['examples'][:5])}）。掃描失敗列 {sc['scan_fail_rows']}。【重算確認】")
w(f"- 決策去重鍵（symbol + 訊號 open_time + side）：唯一鍵 {R['decision_dedupe']['unique_keys']:,}、重複 {R['decision_dedupe']['keys_with_repeats']}，所以原始計數等於去重計數。【重算確認】")
w(f"- 報價：SIGNAL_EVAL {R['quotes']['by_purpose']['SIGNAL_EVAL']:,}、MANAGE {R['quotes']['by_purpose']['MANAGE']}、MANAGE_POLL {R['quotes']['by_purpose']['MANAGE_POLL']}；持倉期間報價最大間隔 " + "、".join(f"{a} {c:.2f}s（{b} 筆）" for a, b, c in R['quotes']['manage_quote_counts_maxgap_sec']) + "。【重算確認】")
w("")
w("### 2.6 可重播程度")
w("")
w(table(["項目", "程度", "依據"], [
    ["live 訊號判定（vol_x/range/ATR/方向/各拒絕碼）", "精確", "封存 K 重算 27,063/27,063 一致"],
    ["進場價、數量、費用、風險估算", "精確", "實錄 bookTicker＋公式，5/5 一致"],
    ["停損更新與出場（本 run 7 筆）", "精確", "實錄 5 秒報價＋封存 ATR，22/22 停損、7/7 出場一致"],
    ["帳務（cash/realized/fees/equity）", "精確", "fills 重播誤差 0"],
    ["反事實：完整 K 上的訊號與進場", "近似", "事後 K＋實錄掃描報價（每檔每分都有 SIGNAL_EVAL 報價）"],
    ["反事實：持倉管理", "近似", "只有 1m K，盤中先後未知，採不利／有利兩種順序"],
    ["盤中逐筆路徑、真實成交、訂單簿深度、資金費", "不可", "未保存 tick／深度；funding_data_gap=True"],
    ["宇宙外 445+ 檔、24h 成交額 <20M 標的", "不可", "未抓取"],
]))
w("")

# ------------------------------------------------------------------ 3
w("## 3. 策略與資金規格（以程式為準）")
w("")
w(table(["項目", "值／規則", "位置"], [
    ["訊號根", "最新已收盤 1m K（S）；range 與 vol MA 皆取 S 之前 20 根、不含 S", "signals.py"],
    ["ATR", "Wilder ATR(14)，取 S 之前 19 根", "market_data.py 326–351（版本不確定）"],
    ["vol_x", "≥ 3.0", "config.VOL_X_MIN"],
    ["突破", "close > range_high + 0.1·ATR（多）／close < range_low − 0.1·ATR（空）", "signals.py"],
    ["價差", "(ask−bid)/mid ≤ 0.0008", "config.MAX_SPREAD"],
    ["估計成交", "ask·(1+0.0002)／bid·(1−0.0002)；須越過突破價（FILL_NOT_BEYOND_BREAKOUT）；追價 ≤ 1 ATR", "signals.py"],
    ["名目／保證金／槓桿", "10,000／1,000／10x；qty = round_step(10000/entry, stepSize)", "config／portfolio.py"],
    ["初始止損", "entry ∓ 1.5·ATR", "portfolio.py"],
    ["單筆風險上限", "估計虧損（含費）≤ 100 U，否則 RISK_BUDGET_EXCEEDED；**不縮止損、不縮部位**", "portfolio.py"],
    ["費用", "每邊 0.05%（SIMULATION_ASSUMPTION）", "config"],
    ["部位上限／冷卻", "同時 3 筆、每標的 1 筆、冷卻 3 分", "config"],
    ["日損鎖", "equity ≤ day_start − 300 時鎖新倉；day_start 於 Asia/Taipei 換日時重設", "engine.py 92–95、account.py 145–150"],
    ["部分停利", "淨浮盈（gross − 出場費）≥ 100 U 時平 30%，再設保本停損", "portfolio.py 386–416"],
    ["移動停利", "extreme ∓ 1.5·ATR_latest，**自進場即生效**（不需先部分停利）；只在每分鐘掃描時更新，5 秒輪詢 atr=None 只檢查觸價", "portfolio.py 422–431"],
    ["反手", "反向訊號先平舊倉、bypass 冷卻再開；30 分內上限 2 次", "engine.py 206–281（版本不確定）"],
    ["候選排序", "vol_x 由大到小、spread 由小到大", "engine.py"],
    ["帳戶", "OPEN：cash −= 保證金+費、realized −= 費；CLOSE：cash −= 出場費、realized += gross−費、全平時退保證金", "portfolio.py 182–185、251、287"],
    ["可用資金判斷", "free_cash() = cash_u（用於 INSUFFICIENT_MARGIN）", "account.py 89、portfolio.py 119"],
]))
w("")
w("- 初始本金 10,000（breakout_follow_v1/config.py 第 30 行，帳戶建立於 02:32:12）。本策略沒有找到任何「上調本金」的紀錄或新數值，標記為 **UNKNOWN**。repo 根目錄 config.py 的 100→500 屬於另一套系統，與本策略無關。【直接紀錄】")
w("")

# ------------------------------------------------------------------ 4
w("## 4. 帳務分離與驗證")
w("")
w(table(["口徑", "數值 (U)", "算法", "標記"], [
    ["帳戶自建立累計已實現", f"{acc['acct_realized']:+.4f}", "account_state.realized_net_u；fills 重播相同", "【重算確認】"],
    ["本 run 已實現增量", f"{realized_inc:+.4f}", f"{acc['acct_realized']:.4f} − 起點 {start['realized_net_u']:.4f}", "【重算確認】"],
    ["本 run 起訖權益變化", f"{win_change:+.4f}", f"{end['equity']:.4f} − {start['equity']:.4f}（起點含浮動 {start['floating_u']:+.4f}）", "【重算確認】"],
    ["本 run 新開 5 筆生命週期淨損益", f"{new_life:+.4f}", "各筆開倉費＋平倉淨額", "【重算確認】"],
    ["跨 run 2 筆在本 run 的貢獻", f"{cross_contrib:+.4f}", f"本 run 平倉淨額 {cross_real_in_run:+.4f} − 起點浮動 {start['floating_u']:+.4f}", "【重算確認】"],
    ["跨 run 2 筆完整生命週期", f"{sum(t['lifecycle_net_total'] for t in cross_tr):+.4f}", "含上一 run 已付開倉費 10.0", "【重算確認】"],
    ["本 run 費用", f"{fees_run:.4f}", f"累計 {acc['acct_fees']:.4f} − 起點 {start['realized_fees_u']:.4f}", "【重算確認】"],
    ["資金費", "0（**缺資料**）", "funding_data_gap=True；未計入", "【直接紀錄】"],
]))
w("")
w(f"- 恆等式：新開生命週期 {new_life:+.4f} ＋ 跨 run 貢獻 {cross_contrib:+.4f} = {new_life+cross_contrib:+.4f} = 權益變化 {win_change:+.4f}。已實現增量 = 新開生命週期 ＋ 跨 run 本 run 平倉 = {new_life+cross_real_in_run:+.4f}。【重算確認】")
w(f"- cash/realized/fees 以 fills 從起點快照重播：cash {acc['replay_cash']:.6f} vs 紀錄 {acc['acct_cash']:.6f}；realized {acc['replay_realized']:.6f} vs {acc['acct_realized']:.6f}；fees {acc['replay_fees']:.6f} vs {acc['acct_fees']:.6f}；equity 恆等式（initial + realized + floating）最大誤差 {acc['equity_identity_max_err']}。【重算確認】")
w(f"- cash 缺陷：截止時 cash {acc['acct_cash']:.4f}、equity {end['equity']:.4f}、無持倉，差額 {acc['cash_minus_equity_end']:.4f} = −Σgross（8 筆已平倉 gross 合計 {gross_sum:+.4f}）。原因是 close_qty 只做 `cash −= fee`（portfolio.py 251），gross 從未入帳。【重算確認】")
w(f"- 缺陷是否改變開倉：run 期間 cash_u 最低 {acc['min_cash_u_during_run']:.2f}；若改用 equity − used_margin，最低 {acc['min_equity_minus_used_margin']:.2f}；兩者都 ≥ 1,000，INSUFFICIENT_MARGIN 列數 {acc['insufficient_margin_rows']}，最大同時持倉 {acc['max_positions_seen']}。**本 run 沒有任何開倉因此改變。**【重算確認】日損鎖用的是 equity，不受 cash 缺陷影響。【直接紀錄】")
w(f"- 回撤：視窗內（只用本 run 權益快照、不插入初始本金）峰值 {dd['window_max']:.4f}（{hhmmss(dd['window_peak_t'])}）到谷底 {dd['window_min']:.4f}（{hhmmss(dd['window_trough_t'])}），MDD = **{dd['window_mdd']:+.4f}**。可驗證帳戶歷史（pre_overnight 權益快照 02:32:13 = 10,000 起，共 {dd['history_points']} 點）MDD = **{dd['history_mdd']:+.4f}**（{hhmmss(dd['history_peak_t'])} → {hhmmss(dd['history_trough_t'])}）。兩者口徑不同，不可混用。快照最大間隔 {dd['equity_snapshot_gaps_sec_max']:.0f} 秒，分鐘內浮動低點未必被捕捉。【重算確認】")
w("")

# ------------------------------------------------------------------ 5
w("## 5. 逐筆交易（5 筆新開＋2 筆跨 run）")
w("")
w("MFE/MAE 有三種口徑：(a) 持倉期間實錄報價（≤7 秒間隔，可成交側價，最可信）；(b) 事後 1m K「完整持倉分鐘」區間估計；(c) 含進出場當分鐘的上界。(c) 會混入進場前或出場後的極值，**只作參考上界，不作為 MFE**。全部以 gross U 表示。")
w("")
order = ["1000PEPEUSDT", "CROSSUSDT", "WLDUSDT", "HUMAUSDT", "龙虾USDT", "DOGEUSDT", "QNTUSDT"]
tmap = {t["symbol"]: t for t in trades}
rows = []
for s in order:
    t = tmap[s]
    S = int(t["signal_open_time"])
    rows.append([s, t["side"], "跨run" if t["source"] != "THIS_RUN" else "本run", hhmmss(ms_iso(S)), hhmmss(ms_iso(S + 60000)),
                 hhmmss(t.get("decision_time")), hhmmss(t["entry_time"]), f(t.get("quote_age_sec"), 2), f(t.get("kline_delay_sec"), 2),
                 px(t["entry_price"]), f(t["qty"], 0), f(t["notional"]), f(t["entry_fee"], 3)])
w("#### 5.1 進場")
w("")
w(table(["標的", "方向", "來源", "訊號根 open", "訊號根 close", "確認/決策", "成交", "報價齡 s", "K延遲 s", "成交價", "qty", "名目", "開倉費"], rows))
w("")
rows = []
for s in order:
    t = tmap[s]
    ba = t.get("signal_quote_bid_ask")
    if ba:
        slip = (t["entry_price"] - ba[1]) * t["qty"] if t["side"] == "LONG" else (ba[0] - t["entry_price"]) * t["qty"]
    else:
        slip = None
    p, a = t.get("posthoc_signal_bar") or {}, t.get("archive_signal_bar") or {}
    rows.append([s, f"{t['range_low']:.6g}–{t['range_high']:.6g}", f(t["vol_x"], 3), f(p.get("vol_x"), 3), px(t["atr"]), px(p.get("atr")),
                 f"{(t['atr']/p['atr']-1)*100:+.0f}%" if p.get("atr") else "—", f(t.get("chase_atr"), 3), f(t.get("risk_estimate_u"), 2),
                 f(slip, 2) if slip is not None else "無紀錄", "成立" if posthoc_ok[s] else f"**不成立**（{p.get('side') or '無突破'}）"])
w("#### 5.2 訊號品質（live 封存 K vs 事後完整 K）")
w("")
w(table(["標的", "range", "vol_x live", "vol_x 真值", "ATR live", "ATR 真值", "ATR 偏差", "追價 ATR", "風險估計 U", "進場滑價 U", "完整 K 是否成立"], rows))
w("")
w("跨 run 2 筆的進場在上一 run（2026-11-19T0232_a8c35e7a），決策紀錄不在本快照，參數取自 pre_overnight account_state.json（額外來源）。【直接紀錄】")
w("")
w("#### 5.3 停損更新（實錄 stop_events；全部 kind=TRAILING_STOP）")
w("")
for s in order:
    t = tmap[s]
    rec = t["stop_updates_recorded"]
    if not rec:
        w(f"- **{s}**：初始止損 {px(t['initial_stop'])}，**無任何更新**。" + ("此筆為空單，ATR 上升使 entry+1.5·ATR 高於初始止損，在極值凍結下永遠無法收緊。" if s == "QNTUSDT" else ""))
        continue
    seq = "；".join(f"{hhmmss(r['t'])} {px(r['new'])}" for r in rec)
    w(f"- **{s}**：初始 {px(t['initial_stop'])} → {seq}（共 {len(rec)} 次；重播 {len(t['stop_updates_replayed'])} 次，逐筆一致={t['stop_replay_match']}）")
w("")
w("驗算：WLD 02:51:03 新停損 0.26197983 = 進場 0.2625525 − 1.5 × ATR 0.00038178。用的是進場價而不是當時極值，證明極值未被保存。【重算確認】")
w("")
rows = []
for s in order:
    t = tmap[s]
    ex = t["exits"][-1]
    er = t["exit_replay"]
    gap = None
    if er:
        gap = (er["eff_stop"] - ex["price"]) * t["qty"] if t["side"] == "LONG" else (ex["price"] - er["eff_stop"]) * t["qty"]
    mf = t["mfe_mae_full_bars_posthoc"]
    mi = t["mfe_mae_incl_partial_bars_posthoc"]
    rows.append([s, hhmmss(ex["t"]), ex["reason"], px(ex["price"]), f(ex["gross"]), f(ex["fee"], 3), f(ex["net"]), f(t["lifecycle_net_total"]),
                 f"{t['hold_sec']:.0f}", f(gap, 2) if gap is not None else "—",
                 f"{f(t['mfe_gross_quotes'])}/{f(t['mae_gross_quotes'])}", f"{f(mf[0])}/{f(mf[1])}", f"{f(mi[0])}/{f(mi[1])}",
                 f"{f(t['max_net_unrealized_at_quotes'])} @{hhmmss(t['max_net_unrealized_t'])}"])
w("#### 5.4 出場與路徑")
w("")
w(table(["標的", "出場", "原因", "價格", "gross", "出場費", "平倉淨額", "生命週期淨額", "持有 s", "穿越停損 U", "MFE/MAE 報價", "MFE/MAE 完整K", "MFE/MAE 上界", "最高淨浮盈"], rows))
w("")
w("- 穿越停損 U = (觸發時有效停損 − 實際出場價) × qty（不利方向為正）。出場以觸發當下的可成交側價成交，5 秒輪詢讓價格可越過停損。【重算確認】")
w("- 部分停利／保本：7 筆都未觸發（最高逐報價淨浮盈 < 100）；反手：7 筆持倉期間反向突破列 = 0。【重算確認】")
w("")
w("#### 5.5 分類與修正極值後的反事實（同一批實錄報價）")
w("")
cls = {
    "1000PEPEUSDT": "完整 K vol_x 2.69 < 3，訊號不成立；曾淨浮盈 +37.5（02:59:40），停損未跟隨 → 資料缺陷訊號＋管理缺陷放大虧損",
    "CROSSUSDT": "完整 K 成立；從未浮盈（最高 −9.0 淨）→ 正常的失敗突破",
    "WLDUSDT": "完整 K vol_x 2.48 < 3，不成立；最高淨浮盈 +2.4 → 資料缺陷訊號",
    "HUMAUSDT": "完整 K vol_x 0.90，不成立；ATR 低估 38%；曾淨浮盈 +33.8 → 資料缺陷訊號＋管理缺陷",
    "龙虾USDT": "完整 K 成立（vol_x 6.05）；15 秒觸初始止損（止損距 0.29%，ATR 低估 18%）→ 訊號成立但止損過窄",
    "DOGEUSDT": "完整 K 勉強成立（3.06）；追價 0.98 ATR 接近上限；15 秒觸止損 → 追價過高＋止損過窄",
    "QNTUSDT": "完整 K vol_x 2.00，不成立；曾淨浮盈 +41.2（03:07:30）；停損從未移動，17 分後回到初始止損 → 資料缺陷訊號＋管理缺陷",
}
rows = []
for s in order:
    t = tmap[s]
    a, b = t["exit_replay"], t["exit_replay_extreme_fixed"]
    rows.append([s, cls[s], f"{hhmmss(a['t'])} {f(a['gross'])}", f"{hhmmss(b['t'])} {f(b['gross'])}" if b else "—", f(fixed_delta.get(s, 0), 2)])
w(table(["標的", "分類", "照程式重播 出場/gross", "修正極值 出場/gross", "差額"], rows))
w("")
w(f"- 修正極值後 gross 合計改善 {fixed_total:+.2f} U（費用相同）。只看單筆路徑，不含釋放部位、冷卻、日損鎖時點的連鎖效應。【推估】")
w("")

# ------------------------------------------------------------------ 6
w("## 6. 全標的覆蓋與候選／拒絕統計")
w("")
w("### 6.1 漏斗（live，原始＝去重；只記錄第一個拒絕原因）")
w("")
raw = R["decision_raw_counts"]
w(table(["final_action", "first_reject_code", "列數"], [[a, b or "—", f(c)] for a, b, c in raw]))
w("")
w("- 分母：評估 27,063 → vol_x 通過 2,408 → 突破成立 814 → 價差／追價／成交越過突破全過 272（OPEN 5、RISK 1、MAX_POSITIONS 1、DAILY_LOSS_LOCK 265）。【重算確認】")
w("- 只記錄 **第一個** 拒絕原因：例如 VOL_X 列不會再檢查突破、價差、追價，所以各後段拒絕數不等於「該條件單獨會擋掉多少」。【直接紀錄】")
lk = R["lock"]
w(f"- 日損鎖：權益首次觸鎖 {hhmmss(lk['first_equity_locked'])}，第一列 DAILY_LOSS_LOCK 決策在 {hhmmss(lk['first_lock_decision'])}。")
w(f"  - 鎖前：{', '.join(f'{b or a} {c}' for a, b, c in lk['pre_lock_unique'])}")
w(f"  - 鎖後：{', '.join(f'{b or a} {c}' for a, b, c in lk['post_lock_unique'])}")
w("- 完整 K 下同規則的漏斗（事後參考）：" + "、".join(f"{k} {v:,}" for k, v in R["posthoc_rule_funnel"].items()) + "。【重算確認】")
w("")
w("### 6.2 候選前瞻報酬（**機會研究，非可實現收益**）")
w("")
w("- 入場價用該候選掃描時實錄 bookTicker 推出的估計成交價（已含 2 bps 不利滑價）；從 S+1 那根起算 N 分，用事後完整 K 收盤；淨值扣 12 bps（兩邊費 10 + 出場滑價 2）。MFE/MAE 用事後 1m 高低，第一根含進場前約 2–5 秒，屬區間估計。【推估】")
w("")


def opp_rows(d):
    rows = []
    for k, v in d.items():
        st = v["stats"]
        cells = [k, f(v["n_candidates"])]
        for h in ("5", "15", "30", "60"):
            s = st.get(h, {})
            if not s.get("n"):
                cells.append("—")
                continue
            cells.append(f"n={s['n']} 淨{s['net_after_cost_mean_bps']:+.1f} 中位{s['median_bps']:+.1f} 勝{s['win_rate']:.0%} MFE{s['mfe_median_bps']:.0f}/MAE{s['mae_median_bps']:.0f}")
        rows.append(cells)
    return rows


w("live 判定的候選（依 first_reject_code 分組；bps）：")
w("")
w(table(["分組", "候選數", "5 分", "15 分", "30 分", "60 分"], opp_rows(R["opportunity_live_candidates"])))
w("")
w("完整 K 重算規則的候選（事後參考）：")
w("")
w(table(["分組", "候選數", "5 分", "15 分", "30 分", "60 分"], opp_rows(R["opportunity_posthoc_rule"])))
w("")
w("- 所有通過突破的 live 候選（814）在每個期間的平均淨報酬都為負；被日損鎖擋下的 265 筆也是負值。SPREAD 組（68 筆）5–30 分為正，但樣本小且價差寬，實際成交成本更高。【推估】")
w("- 未覆蓋範圍：只看宇宙內 65–70 檔（全部 517 個 USDⓈ-M 合約中的一小部分），24h 成交額 < 20M 的標的與宇宙刷新空檔（最長 11 分）沒有觀測；VOL_X/NO_BREAKOUT 被拒的 26,249 列沒有入場價，不做前瞻。【直接紀錄】")
w("")

# ------------------------------------------------------------------ 7
w("## 7. 部分停利／保本／移動停利／反手 驗證")
w("")
w(table(["機制", "程式有分支", "測試通過", "本 run 實際觸發", "說明"], [
    ["部分停利 30% @淨 100 U", "是（portfolio.py 386–416）", "是（test_partial_tp_once_and_breakeven_tighten，隔離執行）", "0", "7 筆最高逐報價淨浮盈 +41.19（QNT），完整 K 區間估計最高 gross +73.24（QNT）扣費後仍 < 100"],
    ["保本停損", "是（同上 403–415）", "是（同一測試）", "0", "依賴部分停利"],
    ["移動停利", "是（422–431）", "**無專屬測試**", "22 次更新、5 次 TRAILING_STOP 出場", "**語意錯誤**：極值未保存，實際是 entry ∓ 1.5·ATR_latest；5 次出場全虧"],
    ["反手", "是（engine.py 206–281，版本不確定）", "是（test_reverse_close_keeps_flat_if_reopen_fails，只測 portfolio 層）", "0", "7 筆持倉期間反向突破決策列 = 0，條件從未成立"],
]))
w("")
w("實際管理時點：MANAGE（分鐘掃描，帶 ATR）共 52 次、MANAGE_POLL（5 秒輪詢，atr=None）共 613 次；停損只在 MANAGE 時更新，與 5.3 節逐筆一致；輪詢間隔最大 6.99 秒。【重算確認】")
w("")
w("離線探針輸出（`defect_probe.py`，cwd=test_sandbox，不觸及 live 目錄）：")
w("")
w("```")
w(PROBE_TXT.strip())
w("```")
w("")

# ------------------------------------------------------------------ 8
w("## 8. 基準重播與敏感度測試")
w("")
w("### 8.1 基準重播（逐列比對）")
w("")
rs, re_ = R["replay_signal"], R["replay_entry"]
w(table(["項目", "比對數", "一致", "不一致", "說明"], [
    ["訊號指標（vol_x、range、ATR、方向、拒絕碼）", f(rs["checked"]), f(rs["match"]), f(rs["mismatch"]), f"缺 K {rs['no_bar']}"],
    ["進場檢查（價差、追價、越過突破、估計成交）", f(re_["checked"]), f(re_["match"]), f(re_["mismatch"]), f"缺報價 {re_['no_quote']}"],
    ["停損更新（極值凍結模型）", "22", "22", "0", "7 筆逐筆"],
    ["出場時間與價格", "7", "7", "0", "實錄報價"],
]))
w("")
v0 = sim["V0_archive_bars_as_coded(live-seen data)"]["adverse_first"]
w(f"- 照程式在封存 K 上模擬（V0）：開倉 {v0['opens']}（同樣是 WLD、HUMA、龙虾、DOGE、QNT，時間相同），觸鎖 {hhmmss(v0['lock_at'])}（live 03:02:03），拒絕計數與 live 完全相同。期末權益 {v0['equity_end']:.2f}（live {end['equity']:.2f}），差額來自 1m K 管理與 5 秒報價管理的不同。【重算確認】")
w("")
w("### 8.2 敏感度測試（**單夜、樣本內**，一次只改一項）")
w("")
w("- 共同條件：起點複製 run 起點（realized −72.3555、PEPE/CROSS 兩筆持倉照原停損與極值）；入場用實錄掃描報價＋2 bps 滑價；費用每邊 0.05%；風險上限 100 U 不縮止損；最大 3 筆、冷卻 3 分、反手上限；部分停利在門檻價成交。")
w("- 盤中先後：不利優先（先檢查停損）與有利優先兩種排列，表中「A/F」分別代表兩者；兩者都不是 5 秒管理的嚴格上下界。")
w("- B0 = 事後完整 K＋照程式（極值凍結）；B1 = B0 只修正極值保存。後面的「B0+」「B1+」各只改一個參數。日損鎖=none 只是離線反事實，**不建議解除運行中的鎖**。")
w("")
rows = []
for n, v in sim.items():
    a, fv = v["adverse_first"], v["favorable_first"]
    rows.append([n, f"{a['opens']}/{fv['opens']}", f"{a['realized_net']:+.1f}/{fv['realized_net']:+.1f}", f"{a['floating_end']:+.1f}/{fv['floating_end']:+.1f}",
                 f"{a['equity_end']:.1f}/{fv['equity_end']:.1f}", f"{a['fees']:.1f}", f"{a['mdd_1m_close']:.1f}/{fv['mdd_1m_close']:.1f}",
                 f"{a['wins']}-{a['losses']} / {fv['wins']}-{fv['losses']}", f"{a['partial_tp_count']}/{fv['partial_tp_count']}",
                 f"{a['reverse_count']}/{fv['reverse_count']}", f"{hhmmss(a['lock_at'])}/{hhmmss(fv['lock_at'])}"])
w(table(["變體", "開倉 A/F", "已實現 A/F", "期末浮動 A/F", "期末權益 A/F", "費用 A", "MDD(1m收) A/F", "勝-負 A/F", "部分停利 A/F", "反手 A/F", "觸鎖 A/F"], rows))
w("")
w("解讀：【推估】")
w(f"- B0 在完整 K 上會開不同的單（如 PIEVERSE、ZEC、BTC），且出現 2 次部分停利、1 次反手，顯示 live 0 次觸發主要是被資料缺陷擋住。")
w("- B1（修正極值）並沒有讓整晚變正：更早出場、釋放部位，於是開更多單、付更多費用，最後一樣觸鎖。本規則在這一夜的完整 K 上沒有正期望。")
w("- 「日損鎖=none」：B0 期末權益看似較高，但那是 MMT 空單在極值凍結下抱了 4 小時、留到期末的浮動 +821 U，已實現是 −1,018/−600。B1 已實現 −2,201/−3,087。這表示日損鎖在本夜有保護作用。")
w("- 其他單參數變動相對各自基準都落在約 ±80 U 內，而且常隨盤中排列反轉方向，屬於雜訊等級，**不支持任何調參**。")
w("")

# ------------------------------------------------------------------ 9
w("## 9. 建議（未實作，僅提案）")
w("")
w("### P0-1 K 線合併保留未收盤快照")
w("- 證據：2.4 節（測試段 58.6% 的 K 與收盤值不同；ATR 中位數低估 24%；7 筆中 4 筆在完整 K 上不成立）。【重算確認】")
w("- 位置：`breakout_follow_v1/market_data.py` 255–261（`concat([cached, fresh]).drop_duplicates(subset=['open_time'])`，版本不確定但行為已由資料佐證）；`breakout_follow_v1/run_store.py` 242–249（封存同樣 keep-first）與 252（`is_closed` 無條件 True）。")
w("- 實際影響：本 run 全部訊號、ATR、止損距離都建立在失真 K 上。潛在影響：任何依賴此 vault 的策略都失真。")
w("- 提案：合併改為新資料覆蓋舊資料（keep='last'，或依 close_time 只接受已收盤的新版本）；寫入 vault 前丟掉 close_time ≥ now 的 K；封存依真實 close_time 標記 is_closed，並在後續收盤版本出現時更新。")
w("- 驗收：(1) 單元測試：快取有未收盤 K、fresh 有收盤 K → 合併結果等於收盤 K；(2) 回放測試：同一時段封存 K 與事後 REST K 的已收盤部分 100% 相等；(3) 線上 data_quality：每 N 分抽查最新已收盤 K 與 REST 再抓一次比對，不一致即告警。")
w("")
w("### P0-2 移動停利極值未保存")
w("- 證據：22/22 停損更新與 7/7 出場被「極值凍結」重播逐筆重現；探針確認。【重算確認】")
w("- 位置：`breakout_follow_v1/portfolio.py` 379–384（更新 extreme）與 418（`pos = get_position(st, symbol)` 丟掉更新）；397 行部分停利後重讀也有同樣問題（雜湊與 run 一致）。")
w(f"- 實際影響：5 筆 TRAILING_STOP 全部虧損出場；同一報價下修正極值可改善 gross 約 {fixed_total:+.2f} U。【推估】潛在影響：獲利單永遠不會被移動停利鎖利。")
w("- 提案：更新極值後立即 `set_position`，或後段不重讀、沿用同一物件；停損事件加記 extreme、atr_latest 欄位以便稽核。")
w("- 驗收：(1) 價格單調上升的多單，每次 MANAGE 後保存的 extreme 等於最高可成交價，停損 = extreme − 1.5·ATR；(2) 空單對稱；(3) 部分停利後 extreme 不回退；(4) 重啟後 extreme 保留；(5) 本報告 `defect_probe.py` P1 改為輸出「extreme updates」。")
w("")
w("### P1-1 帳務：cash 未計入 gross")
w(f"- 證據：cash − equity = {acc['cash_minus_equity_end']:.4f} = −Σgross。【重算確認】位置：`portfolio.py` 182–185、251、287；`account.py` 83（equity_mark）、89（free_cash）。")
w("- 實際影響：本 run 0（最低可用 6,777.96 ≫ 1,000，0 列 INSUFFICIENT_MARGIN）。潛在影響：虧損期 cash 高估可用資金（目前高估 251.25），獲利期則低估、誤擋開倉。")
w("- 提案定義：wallet_balance = initial_capital + Σ(gross − fees − funding)；used_margin = Σ 持倉保證金（部分平倉時按剩餘 qty 比例）；**available = wallet_balance + min(0, floating) − used_margin**（浮虧扣、浮盈不加）。CLOSE/CLOSE_PARTIAL：wallet += gross − fee；釋放對應保證金。")
w("- 遷移：一次性 migration，以 fills 重播重算 wallet（= initial + realized_net，目前 9,668.78），寫入 migration 事件（前後值、來源 fills 雜湊、版本號）；以 version 旗標保證只套用一次（冪等），重啟不重做；不重扣費用（費用已在 realized 內）；不改歷史 fills。")
w("- 驗收：(1) 任一時點 wallet == initial + realized；(2) 無持倉時 available == equity；(3) 部分平倉 30% 後 gross 只入帳一次、保證金按比例釋放；(4) 重啟兩次結果相同；(5) 對本 run fills 重播，遷移後 cash = 9,668.7822。")
w("")
w("### P1-2 測試污染 live 目錄")
w("- 證據：根目錄 fills.jsonl 的 `t:AAAUSDT`／`t:BBBUSDT` 4 列（02:31:40）；隔離執行時同一批測試在 sandbox 寫出 fills.jsonl 與 trades.csv。【直接紀錄】")
w("- 位置：`tests/breakout_follow_v1/test_breakout_core.py` 168–268（只換 ACCOUNT_PATH，沒換 FILLS_PATH/TRADES_PATH）；`config.py` 16–27 使用相對 cwd 的 `output/`。")
w("- 提案：測試用 tmp 目錄整體替換 ROOT_OUT 與所有衍生路徑；匯出與 review 依 run_id 過濾、排除 `t:` 前綴。驗收：測試前後 live 目錄所有檔案雜湊不變。")
w("")
w("### P1-3 日損鎖基準")
w("- 證據：day_start_equity 固定為帳戶建立時的 10,000（Asia/Taipei 換日才重設），pre-run 虧損吃掉額度，本 run 起點只剩 210.63 U。【直接紀錄】位置：`account.py` 145–150、`engine.py` 92–95。")
w("- 提案：只需確認語意（每日日界、是否以 run 起點為基準），**不建議放寬**（8.2 節反事實顯示鎖在本夜有保護作用）。")
w("")
w("### P1-4 可觀測性缺口")
w("- paper_stdout.log 只到啟動初期；沒有錯誤日誌與獨立反手事件檔；stop_events 沒有 extreme/ATR；權益快照約 60 秒一筆。提案：持續日誌輪替、停損事件加欄位、記錄每根 K 的抓取時間與是否收盤。驗收：下一次 run 匯出 MISSING.md 只剩 tick/深度。")
w("")
w("### P2")
w("- P2-1 參數研究：要在 P0 修好且累積 ≥ 5 夜資料後才做，並以樣本外夜驗證；本夜結果不採用。")
w("- P2-2 宇宙刷新空檔（10–11 分）與覆蓋範圍：評估是否要事件觸發刷新。")
w("- P2-3 資金費：接公開 funding rate 計入持倉成本，消除 funding_data_gap。")
w("- P2-4 部分停利門檻 100 U 相當於 10,000 名目漲 1.05%，在本夜完整 K 的止損距離（約 0.2–0.9%）下很少到達；這要在 P0 修好後的資料上再評估。")
w("")

# ------------------------------------------------------------------ 10
w("## 10. 需要使用者確認的事項")
w("")
w("1. 移動停利應該自進場即生效，還是部分停利（或保本）後才啟用？程式目前是自進場生效。")
w("2. 帳務修正採用的 available 定義（9 節 P1-1）是否同意？遷移是在原帳戶就地執行，還是開新帳戶、新 run？")
w("3. 日損鎖基準：以 Asia/Taipei 自然日、帳戶建立時權益為基準是否為原意？")
w("4. 「上調本金」的新數值：本策略沒有找到任何來源，維持 **UNKNOWN**。請提供指令或數值。")
w("5. P0 修正後是否另開新 run_id（建議是），讓修正前後資料不混用？")
w("6. 是否要定期保存 bookTicker／深度，以便日後精確重播進場以外的時點？")
w("")

# ------------------------------------------------------------------ 11
w("## 11. 證據索引、分析腳本與測試輸出")
w("")
files = ["fetch_posthoc_klines.py", "analyze.py", "sim.py", "atr_volx_ratio.py", "run_tests_isolated.py", "defect_probe.py", "make_report.py",
         "out/results.json", "out/atr_volx_ratio.json", "out/test_output.txt", "out/defect_probe_output.txt", "posthoc_klines/_meta.json"]
rows = []
for x in files:
    p = HERE / x
    rows.append([f"`analysis/breakout_follow_v1_20261119/{x}`", f(p.stat().st_size), sha(p)[:16]])
w(table(["檔案", "bytes", "sha256[:16]"], rows))
w("")
w("- `fetch_posthoc_klines.py`：只呼叫公開 GET /fapi/v1/klines，抓每檔封存首根到截止前已收盤的 K，標記 POSTHOC_EXTERNAL_REFERENCE（非當時輸入）。72 檔、0 錯誤。")
w("- `analyze.py`：盤點／雜湊、K 線品質、宇宙、決策漏斗、基準重播、帳務、逐筆重播（極值凍結與修正兩種）、前瞻報酬，並呼叫 `sim.py`。只匯入 pandas/numpy 與自身常數，**不匯入引擎或下單模組**。")
w("- `sim.py`：1m 反事實模擬器（兩種盤中排列）。`atr_volx_ratio.py`：逐決策列 ATR/vol_x 偏差。")
w("- `run_tests_isolated.py`、`defect_probe.py`：在 `test_sandbox/` 為 cwd 執行（相對路徑 output/ 指向 sandbox），執行前後根目錄 fills.jsonl 都是 8,975 bytes，live 目錄未受影響。會匯入 paper 模組（portfolio/account/signals/safety），safety 阻擋所有私有／下單 URL。")
w("- 重現：`cd /Users/alan/MyProjects/automax && python3 analysis/breakout_follow_v1_20261119/analyze.py && python3 analysis/breakout_follow_v1_20261119/atr_volx_ratio.py && python3 analysis/breakout_follow_v1_20261119/make_report.py`")
w("")
w("### 測試結果")
w("")
w(table(["項目", "狀態", "說明"], [
    ["test_breakout_core.py 9 個測試", "**通過**（9/9，隔離目錄）", "目前檔案版本；portfolio/signals 雜湊與 run 一致"],
    ["defect_probe P1（極值保存）", "**失敗**＝缺陷確認", "extreme 停在進場價"],
    ["defect_probe P2（K 線合併）", "**失敗**＝缺陷確認", "保留舊快取列"],
    ["基準重播（訊號／進場／停損／出場）", "通過", "27,063／814／22／7 全一致"],
    ["帳務重播", "通過", "誤差 0"],
    ["移動停利專屬單元測試", "**未執行（不存在）**", "需依 P0-2 新增"],
    ["K 線合併單元測試", "**未執行（不存在）**", "需依 P0-1 新增"],
    ["engine.py 反手整合測試", "**未執行**", "需啟動引擎，違反唯讀限制"],
    ["pytest", "**未執行**", "環境未安裝 pytest，改用逐函式執行器"],
]))
w("")
w("```")
w(TEST_TXT.strip())
w("```")
w("")

text = "\n".join(L) + "\n"
target = PACK / "FULL_OVERNIGHT_ANALYSIS.md"
if target.exists():
    target = PACK / f"FULL_OVERNIGHT_ANALYSIS_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}.md"
target.write_text(text, encoding="utf-8")
print(target, target.stat().st_size)
