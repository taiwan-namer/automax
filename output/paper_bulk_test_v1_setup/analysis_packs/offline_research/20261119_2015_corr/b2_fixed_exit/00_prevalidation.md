# 資料前置驗證（只讀；未部署／未啟動 paper／未改帳戶）

## 5) Paper 停止證據與未平倉
- `paper.pid` 存在: **False**
- 本 profile `paper_bulk_test_v1` / `paper_main` 進程: **0**（另有無關的 `breakout_follow_v1.paper_main` PID 33829，stdout 指向 breakout_follow_v1，**非本帳戶**）
- `account_state.json` mtime ≈ 2026-11-19 20:18（與最後 manage 對齊後靜止）
- 最後 MANAGE_BATCH_COMPLETE: **2026-11-19T20:17:59+08:00** status=OK purpose=MANAGE_POLL
- 未平倉（只讀，未平倉／未改）:
  - **FETUSDT** LONG tid=`20261119T161354_5cb8369a:FETUSDT:1bd4e0c0cc` qty=77264.0 entry=0.12942588 entry_time=2026-11-19T19:33:15+08:00
    initial_stop=0.1291590154681085 effective_stop=0.1291590154681085 trailing_stop=None be=None partial=False
  - **1000PEPEUSDT** SHORT tid=`20261119T161354_5cb8369a:1000PEPEUSDT:f30831260d` qty=2559889.0 entry=0.00390641856 entry_time=2026-11-19T20:07:13+08:00
    initial_stop=0.003927802200094158 effective_stop=0.003927802200094158 trailing_stop=None be=None partial=False
- 結論：維持停止狀態；兩筆未平倉保留；研究截止估值不偽裝成實盤平倉。

## 1) 特徵來源（決策當時 vs 離線重算）
| 欄位 | 決策當時原始紀錄 | 離線 K 線重算 |
|------|------------------|---------------|
| vol_x | SIGNAL_EVAL / OPEN | — |
| spread | SIGNAL_EVAL | — |
| atr | OPEN.atr（決策時寫入） | — |
| bid/ask | decision_snapshot.signal（本批 108/108 皆命中） | — |
| est_fill | SIGNAL_EVAL.est_fill ≡ OPEN.price | — |
| signal_open_time | OPEN / SIGNAL_EVAL | vault open_time |
| signal_close_time | open_time+59999 | vault close_time |
| 資料可用時間 | — | closed → close_time+1；否則 fetched_at |
| 實體/影線/事前趨勢 | — | 僅 closed_flag=True 棒 |
| post_*_ret | — | 見公式；缺失不填補 |

## 2) post_1m/3m/5m_ret
- t = floor(entry_ts → 1m open_time)
- LONG: `(close[t+n]-close[t])/close[t]`；SHORT: `(close[t]-close[t+n])/close[t]`
- 價格基準：進場分鐘收盤 vs t+n 收盤；時間對齊 1m open_time
- **不可用最後可用價填補**；缺棒或缺 closed → null（本批 1/3/5m 缺失數皆 0）

## 3) 研究 K 線疑點（分開列示，不回寫）
- unclosed_snapshot_in_vault: **38**（符號分布：{'ONDOUSDT': 1, 'TAOUSDT': 1, 'KAVAUSDT': 1, 'XRPUSDT': 1, 'EVAAUSDT': 1, 'ICPUSDT': 1, 'AKEUSDT': 1, 'ICNTUSDT': 1, 'JUPUSDT': 1, 'HEIUSDT': 1, 'JCTUSDT': 1, 'QNTUSDT': 1, 'PENGUUSDT': 1, 'VVVUSDT': 1, 'HBARUSDT': 1, 'LITUSDT': 1, 'PIPPINUSDT': 1, 'PUMPUSDT': 1, '1000PEPEUSDT': 1, 'XPLUSDT': 1, 'ZROUSDT': 1, 'ASTERUSDT': 1, 'BREVUSDT': 1, 'HUMAUSDT': 1, 'DOTUSDT': 1, 'INJUSDT': 1, 'HYPEUSDT': 1, 'NIGHTUSDT': 1, 'ENAUSDT': 1, 'STRKUSDT': 1, 'NEARUSDT': 1, 'PHAROSUSDT': 1, 'FILUSDT': 1, 'ETHUSDT': 1, 'LINKUSDT': 1, 'ETHFIUSDT': 1, 'BLESSUSDT': 1, 'AAVEUSDT': 1}）→ 回放路徑 **只用 closed_flag=True**，未閉合棒不參與觸發
- duplicate_open_time / keep-first 衝突: **0**
- closed 但 fetched_before_close: **0**
- 詳表: `00_kline_suspects.csv`

## 4) 止損偏離（U / bps / ATR·R）
- 初始止損：對已平倉樣本算 `vs_initial_stop_{u,bps,atr}`（見 `00_feature_provenance.csv`）
- 追蹤止損：fills **無退出當下有效止損** → 全部標記 `trail_stop_gap`（已平倉 106；未平倉 2 無 close）
- 初始 vs 追蹤分開；無有效退出止損者不捏造

## 閘門
- opens=108；基本缺失=0；硬阻擋=無 → **允許進入 B2 條件回放**
- 成交簿：108/108 snapshot（非 proxy）；fill 重建誤差 max=0 bps
