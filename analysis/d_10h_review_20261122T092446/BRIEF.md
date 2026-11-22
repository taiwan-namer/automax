# BRIEF — Strategy D 紙上復盤（d_quiet_short_v1）

> 唯讀分析。快照截止 `2026-11-22T09:24:54.432668+08:00`（+08）。**不是交易所成交**；模擬假設見 STRATEGY_SPEC／DATA_AUDIT。查無則 N/A。

## 0. 結論摘要

- D 是 **paper 安靜區間做空**：15m 已收盤四棒夠「安靜」→ 高於 P 的限價空單；TP/SL 以 **毛利 USDT**（+15／−3）觸發，另有波動放大 `VOL_SPIKE`。
- 本次可用運行約 **6.47 小時**（2026-11-21T14:35:12+08 → 2026-11-21T21:03:11+08），**不足十小時**；主報告=本次全段至截止。
- 已平 **10** 筆全虧；**經濟 cycle 淨合計 -33.5124 U**（已扣進出場費）。帳戶 `realized_net_u`=**-32.0182 U**（口徑=Σclose_net，**未**扣進場費）。殘倉 **HYPEUSDT** 1 筆未平，**不得計入已實現**。
- 帳本 fills↔帳戶對帳 **PASS**（現金=初始+realized_net−全部進場費）；最大缺口：無 D 原生 K 線／無 funding／無真實深度成交重建。
- 證據偏向：**先修執行與出場邏輯的 paper 單一變因對照**，不支持加碼或上實盤。

## 1. 快照截止與運行身分

| 項 | 值 |
|----|----|
| 截止 | 2026-11-22T09:24:54.432668+08:00 |
| strategy | `d_quiet_short_v1` / `d_quiet_short_testnet_20261121` |
| profile / run_id | `testnet_d_quiet_short_v1` / `2026-11-21T1433_040b980f` |
| 模式 | paper (PAPER_FILLS; simulation_assumption) |
| 環境 | paper_bcd_local/data_d — public fapi quotes; no exchange orders |
| 末次 LAUNCH | 2026-11-21T20:15:33.306048+08:00 pid=8848 |
| 帳本更新／崩潰 | 2026-11-21T21:03:11.682901+08:00（log：PermissionError 寫 account_state） |
| 商品 | 多種 USDT-M 永續（本批成交含 ADA/LINK/AVAX/DOGE/WLD/BNB/XLM/XRP/1000SHIB/SOL/HYPE） |

## 2. D 規則摘要（人話）

D 在幣安永續上（本機紙上）找「最近四根 15 分鐘 K 線波幅夠小」的幣，用當下中價 P 上方 0.5% 掛空單；成交後盯毛損益：賺滿約 15U 停利、虧約 3U 停損，若短時波動突然放大也會 `VOL_SPIKE` 出場。同時最多約 10 倉，每倉保證金約 30U×10 倍≈300U 名目。費用按每邊 5bps 模擬；**TP/SL 是美元毛利門檻，不是價格百分比**。

詳細證據表見 `STRATEGY_SPEC.md`。注意：`launch_meta_d.json` 曾記 range/bar=0.02/0.01，與 config 0.015/0.0075 不符；撤單出現 `BAR_AMP_FAIL`，**以 config 為準**並保留差異紀錄。

## 3. 漏斗與績效

| 指標 | 值 |
|------|----|
| 訊號 | 21（不可用成交反推） |
| 掛單 QUEUE | 19 |
| 撤單 | 8（原因 {'QUIET_FAIL:BAR_AMP_FAIL': 1, 'DEPTH_FAIL_DUAL_NEAR_TOUCH': 5, 'PRICE_DROP_FROM_P': 2}） |
| OPEN / 已平 / 未平 | 11 / 10 / 1 |
| 掃描擋單事件 | {'QUEUE_RESULT': 19, 'SIGNAL_BLOCKED_DEPTH': 24, 'SIGNAL_BLOCKED_SLOT_CAP': 2} |
| 勝／負／平（扣成本後） | 0／10／0；勝率 0.000 |
| 毛損益／費用／資金費／淨 | -30.5087／3.0037／N/A／**-33.5124** |
| 均／中位每筆淨 | -3.3512／-3.4151 |
| 均盈／均虧／盈虧比／PF | N/A／-3.3512／N/A／0.0000 |
| 持倉時間均／中位 | 939.9s／711.6s |
| 最大連虧 | 10 |
| 最大回撤 | -33.5124 U（逐筆已實現累計曲線（不含未平浮盈虧）） |
| 出場原因 | {'SL+VOL_SPIKE': {'n': 4, 'sum_net': -14.393740979999949}, 'VOL_SPIKE': {'n': 2, 'sum_net': -5.454848961999977}, 'SL': {'n': 4, 'sum_net': -13.66382258500004}} |

**對帳：** 帳戶 realized_net=-32.0182=Σclose_net；經濟已平淨=-33.5124；現金核對差≈0.00000000。funding=N/A。滑價已在成交價內。

損失結構（合理推論）：本批 **訊號選幣後快速同向不利**（普漲／波動放大）+ **緊 SL(3U)** + **VOL_SPIKE 與 SL 疊加**；費用約 3.0037U 為次要。非「方向寫反」——全部 SHORT 與規格一致（確認事實）。

## 4. K 線關鍵統計

- 借用 paper C vault 1m（D 無 vault）：D 無專屬 vault；借用同環境 paper C 的 1m last OHLC。非 mark；非 D 當下拉取的15m。
- 已平有路徑 n=10；MFE% 中位 0.2167；MAE% 中位 1.3995
- 浮盈後仍虧（gave_back）10 筆
- 固定窗有效樣本：5/15/30/60m = {'5': 11, '15': 11, '30': 11, '60': 10}；順空期末報酬中位% {'5': -0.11337868480725899, '15': -0.05639097744361325, '30': -0.46385156749839823, '60': -0.6797047426143039}
- 觸及 0.25%→1.5% 次數：{'0.25%': 4, '0.50%': 1, '0.75%': 0, '1.00%': 0, '1.50%': 0}（分母 10；觸及≠可實現）

## 5. 代表交易

- 虧：LINKUSDT reason=SL+VOL_SPIKE net=-4.1618U hold=2532.3s entry=2026-11-21T18:50:26.085863+08:00 exit=2026-11-21T19:32:38.421485+08:00 MFE%=0.3217 MAE%=1.6919
- 虧：XLMUSDT reason=SL net=-3.5650U hold=66.2s entry=2026-11-21T19:31:37.623453+08:00 exit=2026-11-21T19:32:43.788452+08:00 MFE%=0.1120 MAE%=1.3116
- 虧：BNBUSDT reason=SL+VOL_SPIKE net=-3.4773U hold=140.0s entry=2026-11-21T19:30:23.646030+08:00 exit=2026-11-21T19:32:43.695452+08:00 MFE%=0.0968 MAE%=1.2458
- 相對較好（仍可能為負）：AVAXUSDT reason=VOL_SPIKE net=-2.6026U
- 相對較好（仍可能為負）：1000SHIBUSDT reason=VOL_SPIKE net=-2.8523U
- 未平：HYPEUSDT entry=2026-11-21T20:15:34.261048+08:00@59.446 acct_mfe=0.2293199999999848 acct_mae=-2.706479999999995 **非已實現**

## 6. 主要問題、證據、下一步

| # | 判斷 | 類型 | 證據 |
|---|------|------|------|
| 1 | 資料短於宣稱「十小時」 | 確認事實 | 首事件→崩潰 ≈6.47h；21:03 PermissionError |
| 2 | 已平全虧、緊 SL／VOL 主導 | 確認事實 | 10/10 負；exit_reason 皆 SL 或 VOL 相關 |
| 3 | paper 樂觀觸價成交 | 確認事實 | simulation_assumption；無排队 |
| 4 | 主損來自路徑／出場閾值，費用次要 | 合理推論 | 毛≈-30.5087 vs 費≈3.0037 |
| 5 | 無原生 K／funding／tick | 資料不足 | vault 空；funding_data_gap |
| 6 | launch_meta_d 閾值與 config 不一致 | 確認差異／影響未完全定量 | 見 STRATEGY_SPEC |

**建議（非下單）：** 保持不動實盤；paper 上做單一變因：(1) SL 3→5U；(2) 關閉或延後 VOL_SPIKE；(3) 修 Windows 寫檔 PermissionError。成功／失敗先訂：新樣本≥30 筆已平、淨期望與回撤優於本基線，且路徑不明比例下降——**樣本內不可宣稱已驗證**。

## 7. 六大問題速答

1. **D 在做什麼？** 紙上「安靜 15m 區間」後掛空，用美元毛利 TP15/SL3（+波動退出）。  
2. **資料可信？** 帳本內部可信（PASS）；經濟含義 PARTIAL（模擬+缺 funding/原生K）。最大缺口：原生行情歸檔與真實成交。  
3. **毛／淨？** 毛 -30.5087U；淨 -33.5124U（已平）。主損：進場後快速不利＋緊停損／波動退出。  
4. **實作疑點？** 未見方向顛倒或費用重扣；見 meta/config 閾值不一致、崩潰停機、殘倉未平。未收盤棒：規格禁止，本批訊號帶 completed bar close_time。  
5. **先不動／修／對照？** 先修寫檔穩定性 + paper 單一變因；不支持上實盤。  
6. **假設：** SL 過緊；VOL_SPIKE 與 SL 同輪傷害；安靜參數在趨勢日失效。判定：預註冊指標後用新時間段驗證。

## 8. 附件與未決

- `STRATEGY_SPEC.md` `DATA_AUDIT.md` `TRADES.csv` `KLINE_ANALYSIS.md` `COUNTERFACTUAL.md` `summary.json` `kline_rows.json` `_d_10h_review.py` `REPRODUCE.md` `snapshots/`
- 未決：HYPE 最終平倉；PermissionError 根因；launch_meta_d 0.02/0.01 是否曾注入進程；真實 15m 歸檔補齊。
