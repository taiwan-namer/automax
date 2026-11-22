# STRATEGY_SPEC — D quiet-short

## 身分（確認事實）
| 項 | 值 | 證據 |
|----|----|------|
| strategy_id | `d_quiet_short_v1` | `config.py` L12 |
| version | `d_quiet_short_testnet_20261121` | config L13；account_state；LAUNCH_META |
| profile | `testnet_d_quiet_short_v1` | config L14；紙上仍用此名 |
| CODE_FIX_TAG | `testnet_d_size_x0_1_20261121` | config L15 |
| 模式 | **paper 模擬**（`PAPER_FILLS`） | LAUNCH_META mode；fills `simulation_assumption`；不下單 |
| 環境 | `paper_bcd_local/data_d`；公開 `fapi.binance.com` 行情 | runtime_root；ALLOWED_PUBLIC_PATHS |
| 方向 | **僅 SHORT** | signals.py / portfolio queue_short_limit |
| 多臂 | 無 | 單一 profile |

## 規則摘要（實際行為）

### 訊號（15m 已收盤）
- 週期：`KLINE_INTERVAL=15m`，僅用**已完成**棒（`evaluate_quiet` 不吃未收盤）。證據：`signals.py` L70–71, L198–199。
- 安靜窗：連續 `QUIET_BARS=4` 根；`range_amp=(hi-lo)/close ≤ RANGE_AMP_MAX(0.015)`；每棒 `(h-l)/open ≤ BAR_AMP_MAX(0.0075)`。`signals.py` L117–128；`config.py` L117–119。
- 評估／掃描：約每分鐘掃描秒=`SCAN_SECOND=5`；管理 `MANAGE_DUE_SEC=5`。`config.py` L125–127。
- 冷卻：出場後 `COOLDOWN_REBUILD`；需新鮮已收盤棒（vol-exit 要 4 根，其他 1 根）。`signals.py` L216–226。
- 同 signal_id 不重複發。`signals.py` L176–178。

### 過濾
- 排除 BTC/ETH；槓桿幣／穩定幣／TradFi 黑名單。`config.py` L89–102。
- 24h quoteVolume ≥ 20M USDT；spread `(ask-bid)/mid ≤ 0.0008`。`config.py` L66–67。
- 深度：近觸 0.1% 內 bid 與 ask 名義各 ≥ 50k USDT。`config.py` L73–85。
- 槽位：最多 10；同幣持倉或掛單則 BUSY。`config.py` L33；`portfolio.py` L132–135。

### 進場
- 限價空：`limit = P × 1.005`（tick 上取整）。`config.py` L109；`portfolio.py` L136–139。
- 無時間 TTL；撤單：`mid ≤ P×0.99` 或安靜／資格失效。`config.py` L110–112；events 見 `QUIET_FAIL:BAR_AMP_FAIL`。
- 成交模型（模擬）：bid≥limit → 以 limit 成交（maker 假設費率仍用 5bps 單邊）。LAUNCH_META fill_model；`FEE_RATE_ONE_SIDE=0.0005`。
- 名目：保證金 `STAKE_USDT=30` × 槓桿 `10` → **約 300U**（config 檔頭註解曾寫 3000U 屬過時／錯誤；以 `CAPITAL_CONFIRM_NOTE` 與實際 notional≈300 為準）。`config.py` L30–42。

### 出場
- **TP/SL 單位是 USDT 毛損益（gross）**，不是價格百分比、也不是保證金報酬率。觸發：`metric=gross` ≥ +15U → TP；≤ −3U → SL。`portfolio.py` L968–978；`config.py` L31–37。
- 另：滾動振幅 ≥ `T=max(VOL_EXIT_FLOOR=0.008, 2×B)` → `VOL_SPIKE`。`portfolio.py` L971–980。
- 可同時列出多觸發（本批見 `SL+VOL_SPIKE`）。
- 出場模擬：吃 ask 側 walk（激進出場）；滑價可反映在成交價；**不可再另扣一層滑價當成本**。LAUNCH_META fill_model_exit。
- 無追蹤停利、無分批 TP、無加倉（本策略）。

### 費用／資金費
- 設定：每側 5bps；SLIP 0.0002 為 book-walk 不可用時後備。`config.py` L47–48。
- 記帳：OPEN 記 entry fee；CLOSE `net_u`＝gross−exit_fee（**不含** entry fee）；完整 cycle 淨＝Σclose_net−entry_fee。funding：`funding_data_gap` → **MISSING／N/A**。

## 文件 vs 實際差異
| 項目 | 文件／meta | 實際證據 | 採納 |
|------|------------|----------|------|
| 名目 | config 頂註「300U margin×10=3000U」 | STAKE=30、成交 notional≈300、CAPITAL_CONFIRM_NOTE | **30×10=300U** |
| 安靜閾值 | `launch_meta_d.json` range=0.02 / bar=0.01 | config 0.015 / 0.0075；撤單 `BAR_AMP_FAIL` | **以 config 載入為準**；meta 數字來源未在啟動腳本覆寫→標差異 |
| Testnet 名 | profile 含 testnet_ | 現行 paper_bcd，無下單 | **paper** |

## 十小時內版本／重啟
- run_id 全程 `2026-11-21T1433_040b980f`（帳本未重置）。
- 活動：約 14:35 起有 QUEUE；18:37 大批掛單；18:49–19:32 成交與集中平倉；20:15 再 LAUNCH 並 OPEN HYPE；**21:03 PermissionError 寫 account_state 崩潰停止**。
- strategy_version 未變。分段敘事：同一版本、多次進程，**不混成無關策略**；績效用同一 run_id 帳本。


## 補充：TP／SL 尺度證據（2026-11-22 supplement）

### 程式證明：每倉（whole-position）毛利 USDT 門檻，非組合／非分腿
- 模組說明：`portfolio.py` L1「whole-position TP/SL/vol-exit」。
- 開倉寫入 `pos["tp_usdt"]=tp_u`、`pos["sl_usdt"]=sl_u`，來自 `scaled_tp_sl(filled_frac)` = `TP_USDT * fill_ratio`／`SL_USDT * fill_ratio`（`portfolio.py` L115–117, L204–247）。
- 觸發：`metric`（該倉 gross 或 net）對照 **該倉** `pos["tp_usdt"]`／`pos["sl_usdt"]`（L975–978），不是帳戶合計。
- 本批成交 `fill_ratio≈1`，故每倉即 **+15U／−3U gross**。HYPE 殘倉帳戶欄位 `tp_usdt=15`/`sl_usdt=3` 可核對。
- 價格換算（SHORT）：`tp_px = entry - tp_usdt/qty`；`sl_px = entry + sl_usdt/qty`（`testnet_hooks.py` L37–51 同式）。
- **無**追蹤停利、**無**時間 TTL 出場、**無**分批 TP；加倉僅在同一 pending 部分成交時縮放門檻（本批皆滿額）。`PARTIAL_TP` 不存在於 D。

### 逐筆 TP／SL 價格與百分比
見 `SUPPLEMENT.md` 表（名目≈300U 時，SL3≈1% 價距、TP15≈5% 量級，隨 entry/qty 微變）。
