# SUPPLEMENT — D 10h 復盤證據補篇

截止（與主報告同一快照）：`2026-11-22T09:24:54.432668+08:00`；行情窗末日=`2026-11-21T21:03:11.682901+08:00`（account.updated_at）。
本輪**未**改交易邏輯、**未**放寬 SL、**未**關 VOL、**未**重啟程式。

## 1. TP／SL 真正尺度

**確認事實：** +15U／−3U 是**每一倉位**的毛損益（gross）觸發門檻，不是組合合計，也不是分腿。
證據：`portfolio.py` 以 `pos["tp_usdt"]`／`pos["sl_usdt"]` 對該倉 `metric` 比較；`scaled_tp_sl` 僅依該筆 fill_ratio 縮放；模組 docstring 寫 whole-position。
本批無動態加倉／分批停利／時間退出（確認事實）。百分比=由 USDT 門檻反推的價格距離，非策略直接參數。

| symbol | notional | tp_usdt/sl_usdt | tp_px / sl_px | tp% / sl% | exit |
|--------|----------|-----------------|---------------|-----------|------|
| ADAUSDT | 299.94 | 15.0/3.0 | 0.132714 / 0.141097 | 5.001% / 1.000% | SL+VOL_SPIKE |
| LINKUSDT | 299.97 | 15.0/3.0 | 7.973302 / 8.476940 | 5.001% / 1.000% | SL+VOL_SPIKE |
| AVAXUSDT | 294.40 | 15.0/3.0 | 6.073913 / 6.465217 | 5.095% / 1.019% | VOL_SPIKE |
| DOGEUSDT | 299.99 | 15.0/3.0 | 0.050540 / 0.053732 | 5.000% / 1.000% | SL |
| WLDUSDT | 299.93 | 15.0/3.0 | 0.209662 / 0.222908 | 5.001% / 1.000% | SL |
| BNBUSDT | 294.89 | 15.0/3.0 | 499.804286 / 531.947143 | 5.087% / 1.017% | SL+VOL_SPIKE |
| XLMUSDT | 299.97 | 15.0/3.0 | 0.118787 / 0.126291 | 5.000% / 1.000% | SL |
| XRPUSDT | 299.93 | 15.0/3.0 | 0.854609 / 0.908598 | 5.001% / 1.000% | SL |
| 1000SHIBUSDT | 300.00 | 15.0/3.0 | 0.003352 / 0.003563 | 5.000% / 1.000% | VOL_SPIKE |
| SOLUSDT | 299.43 | 15.0/3.0 | 65.087506 / 69.206499 | 5.009% / 1.002% | SL+VOL_SPIKE |
| HYPEUSDT | 299.61 | 15.0/3.0 | 56.469810 / 60.041238 | 5.007% / 1.001% | OPEN |

詳細已補入 `STRATEGY_SPEC.md` 補充節。

## 2. 實際生效設定（不得只信現盤 config）

### 載入鏈（確認事實）
- 進程：`python -m d_quiet_short_v1.paper_main --paper --runtime-root ... --allow-fills`（log CMD）。
- CLI **無** RANGE/BAR 參數（`paper_main.py` argparse 僅 paper/runtime-root/hours/once/testnet/scan-only/allow-fills）。
- `paper_main` 寫入的 `LAUNCH_META.json` **不含** range/bar 閾值（只含 fee/slip/fill_model/version）。
- `signals.evaluate_quiet` 讀 `config.RANGE_AMP_MAX` / `config.BAR_AMP_MAX`（`signals.py` L123–127）。
- 啟動器寫入的 `paper_bcd_local/launch_meta_d.json` 含 `range_amp_max=0.02`、`bar_amp_max=0.01`，但 **tools 內找不到** 把它賦值進 `config` 的程式；**不能**證明該檔曾注入進程。
- 現盤 `config.py`：RANGE=0.015、BAR=0.0075。git 對該檔無可用歷史（未追蹤／空 log）→ 無法用 git 證明運行時磁碟值。

### 以訊號反證歷史有效 BAR 閾值（確認事實）
- 訊號數 21；其中 max(bar_amps)**>0.0075** 者 **16** 筆；**>0.01** 者 **0** 筆。
- 觀測 max(bar_amp)=0.009515；max(range_amp)=0.013720。
- 若當時 `BAR_AMP_MAX=0.0075`，`evaluate_quiet` 應拒絕這些訊號；但它們已寫入 `signals.jsonl`。
- 因此：**主交易時段（約 18:37 後）有效 BAR 上限 ≥≈0.00952，且與 launch_meta_d 的 0.01 相容；與現盤 config 0.0075 不相容。**
- RANGE：所有訊號 range_amp≤0.015，**無法**區分有效值是 0.015 還是 0.02 → RANGE 歷史有效值 **未知（兩候選皆可能）**。
- `BAR_AMP_FAIL` 撤單（TRX 14:45）**只證明檢查被觸發**，事件列無實際 amp／threshold → **不能**單獨證明用哪個閾值。

| 時段 | 可證明 | 未知 |
|------|--------|------|
| 14:33 首啟～18:01 崩潰 | 版本 d_quiet_short_testnet_20261121；TP15/SL3（後續持倉欄位） | 該段 BAR/RANGE 數值 |
| 18:36 D-only 重啟～19:41 崩潰 | BAR 有效 ≥0.0095（訊號反證）；TP15/SL3 | RANGE 0.015 vs 0.02 |
| 20:15 ACD 重啟～21:03 崩潰 | 日誌印「毛TP 15.0／毛SL 3.0」；HYPE 帳戶 tp/sl=15/3 | BAR/RANGE 數值（該段幾乎無新 quiet 訊號可反證） |

## 3. PermissionError 與 HYPE 殘倉

### 呼叫鏈（確認事實，三次皆同構）
```
paper_main.main → asyncio.run(run_paper_loop)
  → engine.run_paper_loop → scan_once → manage_all
    → portfolio.save_account → util.atomic_write_json
      → os.replace(tmp, account_state.json)
PermissionError: [WinError 5] 存取被拒。: '.tmp_….json' -> 'account_state.json'
```
時間點：①≈18:01（空手）②≈19:41（HYPE 仍掛單）③≈21:03（HYPE 已持倉）。

### 寫檔方式
- `atomic_write_json`：同目錄 `mkstemp` 寫入→`fsync`→`os.replace` 覆蓋 `account_state.json`。
- 現版另有 WinError 5/32 最多 12 次重試（`util.py` L28–53）；**崩潰當下仍最終 raise**（未吞掉）。
- **可證實：** replace 目標檔被拒絕（WinError 5）。**不能證實**具體占用者（防毒／索引／另一讀寫進程）— 無 handle 診斷日誌 → 根因進程級 **未知**。

### 錯誤後行為（確認事實）
- 例外未在 `manage_all`／`run_paper_loop` 內捕獲 → **整個 Python 進程結束**。
- 之後無掃描、無盯倉、無出場，直到下一次 `===== launch =====`。

### HYPE 殘倉
| 項 | 值 |
|----|----|
| trade_id | 2026-11-21T1433_040b980f:HYPEUSDT:c3ef1cb5fd |
| qty_remaining | 5.04 |
| entry_price / entry_time | 59.446 / 2026-11-21T20:15:34.261048+08:00 |
| margin_u / notional | 30.0 / 299.60784 |
| tp_usdt / sl_usdt（每倉） | 15.0 / 3.0 |
| acct mfe_u / mae_u | 0.2293 / -2.7065 （USDT 毛利口徑，非%） |
| 最後 roll 樣本時間 | 2026-11-21T21:03:11.681901+08:00 |
| 最後 roll px（mid 樣本） | 59.8655 |
| account.updated_at | 2026-11-21T21:03:11.682901+08:00 |

**不得**把截止後行情當成已平倉。浮盈虧單位：帳戶 mfe/mae 為 **U 毛利**；日誌「浮動淨」為估平倉淨（含假設滑價／費）。

### 重啟恢復（確認事實）
- `load_account()` 若 `account_state.json` 存在則整份載入（含 positions/pending），**不**新建 run_id（`portfolio.py` L64–77）。
- 本批 run_id 始終 `2026-11-21T1433_040b980f`。
- 去重：`applied_exchange_trade_keys`／settlement_journal 恢復邏輯存在；paper 成交鍵主要靠本地 trade_id。
- 20:15 重啟時日誌先顯示掛單 HYPE，隨即成交進場 — 殘限價被恢復後觸價。

### 最小修復建議（本輪不實作）
1. `save_account` 失敗時降級：保留 tmp、寫 alert、**不要**讓主迴圈崩掉（或有界重試後跳過本輪保存）。
2. 避免多進程同時讀寫同一 `account_state.json`；鎖外另加「寫入中」偵測。
3. 崩潰復原檢查：pending/position 與 fills 對帳後再允許新訊號。

## 4. 可比較價格路徑（借用 C vault）

- 來源：`c:\Users\Admin\MyProjects\automax\output\paper_bcd_local\data_c\data\testnet_c_inv_v1\kline_vault_v2_clean`（paper C sibling）。
- 價種：**1m last OHLC**；D 訊號用 **15m**、出場盯 **book mid/ask** → **不完全同質**。標 `price_kind=sibling_1m_last_ohlc`。
- 商品集合：本批 11 個成交符號在 C vault **皆有** 1m 檔。
- 進出場當根可能含持倉外價格 → `boundary_error=true`。
- **嚴格完全位於持倉內的整分 K**：短單常為 0 根 → `path_strict_inside.n=0` 標 N/A。

### 逐筆固定窗（重疊 K；MFE/MAE/期末順向%）

**ADAUSDT** hold=2606.8s boundary=True overlap_n=44 strict_n=42 MFE%=0.5011 MAE%=1.4316 MFE_U=1.5029 MAE_U=-4.2940
  - 5m: ret=0.0000% MFE%=0.0716 MAE%=0.1432 bars=6
  - 15m: ret=0.3579% MFE%=0.5011 MAE%=0.1432 bars=16
  - 30m: ret=0.4295% MFE%=0.5011 MAE%=0.1432 bars=31
  - 60m: ret=-0.3579% MFE%=0.5011 MAE%=1.5032 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=False sec=N/A mae%_before=N/A order=N/A
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**LINKUSDT** hold=2532.3s boundary=True overlap_n=43 strict_n=41 MFE%=0.3217 MAE%=1.6919 MFE_U=0.9650 MAE_U=-5.0751
  - 5m: ret=-0.0596% MFE%=0.0477 MAE%=0.1787 bars=6
  - 15m: ret=0.1191% MFE%=0.3217 MAE%=0.1787 bars=16
  - 30m: ret=0.0238% MFE%=0.3217 MAE%=0.2740 bars=31
  - 60m: ret=-0.6791% MFE%=0.3217 MAE%=1.7276 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=False sec=N/A mae%_before=N/A order=N/A
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**AVAXUSDT** hold=1305.7s boundary=True overlap_n=23 strict_n=21 MFE%=0.4688 MAE%=1.5937 MFE_U=1.3800 MAE_U=-4.6920
  - 5m: ret=0.3125% MFE%=0.3125 MAE%=0.0781 bars=6
  - 15m: ret=0.3281% MFE%=0.4688 MAE%=0.0781 bars=16
  - 30m: ret=-0.7500% MFE%=0.4688 MAE%=1.6562 bars=31
  - 60m: ret=-0.6562% MFE%=0.4688 MAE%=1.6562 bars=61

**DOGEUSDT** hold=1294.4s boundary=True overlap_n=23 strict_n=21 MFE%=0.2068 MAE%=1.8233 MFE_U=0.6203 MAE_U=-5.4698
  - 5m: ret=0.0564% MFE%=0.0752 MAE%=0.0752 bars=6
  - 15m: ret=-0.0564% MFE%=0.2068 MAE%=0.0752 bars=16
  - 30m: ret=-0.8835% MFE%=0.2068 MAE%=1.8421 bars=31
  - 60m: ret=-0.9023% MFE%=0.2068 MAE%=1.8421 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=False sec=N/A mae%_before=N/A order=N/A
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**WLDUSDT** hold=1283.1s boundary=True overlap_n=23 strict_n=21 MFE%=0.2266 MAE%=2.1296 MFE_U=0.6795 MAE_U=-6.3873
  - 5m: ret=0.2266% MFE%=0.2266 MAE%=0.0453 bars=6
  - 15m: ret=-0.0906% MFE%=0.2266 MAE%=0.1812 bars=16
  - 30m: ret=-0.8609% MFE%=0.2266 MAE%=2.1296 bars=31
  - 60m: ret=-0.3172% MFE%=0.2266 MAE%=2.1296 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=False sec=N/A mae%_before=N/A order=N/A
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**BNBUSDT** hold=140.0s boundary=True overlap_n=3 strict_n=1 MFE%=0.0968 MAE%=1.2458 MFE_U=0.2856 MAE_U=-3.6736
  - 5m: ret=-0.6362% MFE%=0.0968 MAE%=1.2875 bars=6
  - 15m: ret=-0.6134% MFE%=0.0968 MAE%=1.2875 bars=16
  - 30m: ret=-0.7881% MFE%=0.0968 MAE%=1.2875 bars=31
  - 60m: ret=-1.1660% MFE%=0.0968 MAE%=1.3236 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=False sec=N/A mae%_before=N/A order=N/A
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**XLMUSDT** hold=66.2s boundary=True overlap_n=2 strict_n=0 MFE%=0.1120 MAE%=1.3116 MFE_U=0.3359 MAE_U=-3.9344
  - 5m: ret=-0.4479% MFE%=0.1120 MAE%=1.3116 bars=6
  - 15m: ret=-0.0240% MFE%=0.1120 MAE%=1.3116 bars=16
  - 30m: ret=-0.4639% MFE%=0.1120 MAE%=1.3116 bars=31
  - 60m: ret=-0.7358% MFE%=0.1120 MAE%=1.3116 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=True sec=676.2 mae%_before=1.3116 order=HIT_OBSERVED_ON_1M
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**XRPUSDT** hold=55.2s boundary=True overlap_n=2 strict_n=0 MFE%=0.1223 MAE%=1.3673 MFE_U=0.3667 MAE_U=-4.1008
  - 5m: ret=-0.5225% MFE%=0.1223 MAE%=1.4229 bars=6
  - 15m: ret=-0.3446% MFE%=0.1223 MAE%=1.4229 bars=16
  - 30m: ret=-0.2223% MFE%=0.1223 MAE%=1.4229 bars=31
  - 60m: ret=-0.4558% MFE%=0.1223 MAE%=1.4229 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=False sec=N/A mae%_before=N/A order=N/A
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**1000SHIBUSDT** hold=60.6s boundary=True overlap_n=2 strict_n=0 MFE%=0.1701 MAE%=0.9921 MFE_U=0.5102 MAE_U=-2.9762
  - 5m: ret=-0.1134% MFE%=0.1701 MAE%=0.9921 bars=6
  - 15m: ret=-0.3118% MFE%=0.1701 MAE%=0.9921 bars=16
  - 30m: ret=-0.1417% MFE%=0.1701 MAE%=0.9921 bars=31
  - 60m: ret=-0.6803% MFE%=0.1701 MAE%=0.9921 bars=61
  - ERRATA(2026-11-22): 若任何衍生報告文字將本標的 **30m bars 誤寫為 61**，應更正為 **31**。底層 `supplement_data.json` 與重算均為 30m=31／60m=61；屬報告文字錯誤，非資料錯誤。

**SOLUSDT** hold=55.1s boundary=True overlap_n=2 strict_n=0 MFE%=0.2919 MAE%=1.2405 MFE_U=0.8740 MAE_U=-3.7145
  - 5m: ret=-0.3795% MFE%=0.2919 MAE%=1.2697 bars=6
  - 15m: ret=-0.4232% MFE%=0.2919 MAE%=1.2697 bars=16
  - 30m: ret=-0.2773% MFE%=0.2919 MAE%=1.2697 bars=31
  - 60m: ret=-0.8173% MFE%=0.2919 MAE%=1.2697 bars=61
  - SL 後回撤／達標（自出場起，1m；順序可能不明）：
    - back_to_entry: hit=False sec=N/A mae%_before=N/A order=N/A
    - breakeven_cost: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_0.5pct: hit=False sec=N/A mae%_before=N/A order=N/A
    - fav_1.0pct: hit=False sec=N/A mae%_before=N/A order=N/A

**HYPEUSDT** hold=2857.4s boundary=True overlap_n=49 strict_n=47 MFE%=0.1060 MAE%=0.9639 MFE_U=0.3175 MAE_U=-2.8879
  - 5m: ret=-0.4761% MFE%=0.1060 MAE%=0.8461 bars=6
  - 15m: ret=-0.0505% MFE%=0.1060 MAE%=0.8461 bars=16
  - 30m: ret=-0.7250% MFE%=0.1060 MAE%=0.9639 bars=31
  - 60m: CENSORED

### 進場聚集與持倉重疊
- 尖峰同時持倉數（含未平至窗末）：**10**
- 進場分鐘聚集（≥2）：
  - 2026-11-21 19:10: ['AVAXUSDT', 'DOGEUSDT', 'WLDUSDT']
  - 2026-11-21 19:31: ['XLMUSDT', 'XRPUSDT', '1000SHIBUSDT', 'SOLUSDT']
- **合理推論：** 19:30–19:32 多倉同時開／平，損失高度集中在同一小波段；非完全獨立樣本。

## 5. 固定進場局部診斷 A／B／C

> **不是完整策略回測。** 固定原進場；不重建不同退出對後續槽位／冷卻／進場的影響。
> **VOL_SPIKE 無法用 1m OHLC 精準重建**（需 mid 滾動 15m 振幅）。因此：
> - A/B/C 的 K 線模型只跑 **TP／SL 價格觸及**；
> - 凡實際含 VOL 的成交，A 的「重現」預期系統性偏差；
> - C「移除 VOL」= 在此模型下與 A 相同屏障，**不能**捏造「若無 VOL 的成交價」，只能對照實際原因標籤。

### A 重現誤差（OHLC SL/TP vs 實際）

| symbol | actual_reason | model_A | px_err | net_err | VOL in actual? |
|--------|---------------|---------|--------|---------|----------------|
| ADAUSDT | SL+VOL_SPIKE | SL | -0.000003 | 0.0058 | True |
| LINKUSDT | SL+VOL_SPIKE | SL | -0.024060 | 0.8603 | True |
| AVAXUSDT | VOL_SPIKE | SL | 0.015065 | -0.6933 | True |
| DOGEUSDT | SL | SL | -0.000018 | 0.1015 | False |
| WLDUSDT | SL | SL | -0.000092 | 0.1258 | False |
| BNBUSDT | SL+VOL_SPIKE | SL | -0.322857 | 0.1809 | True |
| XLMUSDT | SL | SL | -0.000110 | 0.2635 | False |
| XRPUSDT | SL | SL | 0.000098 | -0.0328 | False |
| 1000SHIBUSDT | VOL_SPIKE | SL | 0.000005 | -0.4492 | True |
| SOLUSDT | SL+VOL_SPIKE | SL | -0.033501 | 0.1465 | True |

### 匯總（模型已平淨；未平另列）
- A TP15/SL3：已平 n=10 淨合計=-33.0034；未平=1（若出場估淨合計 -1.7720）；歧義=0
- B TP15/SL5：已平 n=7 淨合計=-37.1061；未平=4（估 -9.3595）；歧義=0
- C 無VOL標籤對照／同 TP15/SL3 模型：已平 n=10 淨合計=-33.0034（與 A 同模型屏障）
- 實際已平經濟淨（基線）：-33.5124

### 放寬 SL（B vs A 模型）逐筆效應計數
{'LARGER_LOSS': 7, 'EXTENDED_OPEN_AT_CUTOFF': 3}

| symbol | B_effect | A_net | B_net | actual_net |
|--------|----------|-------|-------|------------|
| ADAUSDT | LARGER_LOSS | -3.3014 | -5.3024 | -3.3072 |
| LINKUSDT | LARGER_LOSS | -3.3015 | -5.3025 | -4.1618 |
| AVAXUSDT | LARGER_LOSS | -3.2959 | -5.2969 | -2.6026 |
| DOGEUSDT | LARGER_LOSS | -3.3015 | -5.3025 | -3.4030 |
| WLDUSDT | LARGER_LOSS | -3.3014 | -5.3024 | -3.4272 |
| BNBUSDT | LARGER_LOSS | -3.2964 | -5.2974 | -3.4773 |
| XLMUSDT | EXTENDED_OPEN_AT_CUTOFF | -3.3015 | N/A | -3.5650 |
| XRPUSDT | EXTENDED_OPEN_AT_CUTOFF | -3.3014 | N/A | -3.2687 |
| 1000SHIBUSDT | EXTENDED_OPEN_AT_CUTOFF | -3.3015 | N/A | -2.8523 |
| SOLUSDT | LARGER_LOSS | -3.3009 | -5.3019 | -3.4474 |
| HYPEUSDT | OPEN_RESIDUAL | OPEN_AT_CUTOFF | OPEN_AT_CUTOFF | OPEN |

**解讀約束：** 即使 B 顯示「救回」或「更大虧損」，也只是固定進場＋1m 觸價假設；**不能**因十筆全虧就預設 SL 太緊，也**不能**把「再跑 30 筆」當驗證通過門檻。

## 確認事實／推論／未知

### 確認事實
| 項 | 內容 |
|----|------|
| TP/SL 尺度 | 每倉 gross ±15/±3 U（滿倉時）；非組合 |
| 無時間退出／無分批 TP | 程式與本批成交一致 |
| PermissionError 鏈 | manage_all→save_account→os.replace；進程終止 |
| 崩潰後無監控 | 需人工／腳本重啟才恢復 |
| 歷史 BAR 非 0.0075 | 多筆訊號 max_bar_amp∈(0.0075,0.01] 仍發出 |
| HYPE 殘倉 | qty=5.04 @59.446；tp/sl=15/3；未平 |
| 帳本對帳 | 見主 BRIEF；本輪未改數字口徑 |

### 合理推論
| 項 | 內容 |
|----|------|
| 18:36 後 BAR≈0.01 | 與 launch_meta_d 一致，但注入路徑未找到 |
| 損失聚集 | 19:30 前後同波多倉重疊 |
| 主損驅動 | 進場後快速逆向接近/超過 SL 價距；VOL 常與 SL 同列 |
| WinError 5 | 檔案被其他句柄占用的典型表現，但占用者未鑑定 |

### 未知
| 項 | 缺什麼 |
|----|--------|
| RANGE 歷史有效值 | 0.015 vs 0.02 無區分性訊號 |
| 14:33–18:01 段 BAR 值 | 無足夠 >0.0075 訊號反證 |
| launch_meta_d 寫入者 | 倉庫 tools 無對應賦值碼 |
| PermissionError 占用進程 | 無 handle/ETW 證據 |
| VOL 精準反事實 | 缺當時 mid 滾動序列 |
| 同根 TP/SL 先後 | 僅 1m OHLC |
| HYPE 最終結果 | 崩潰後未平；截止後行情不當平倉 |

附件：`supplement_data.json`（本腳本結構化輸出）。