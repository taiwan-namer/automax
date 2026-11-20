# 分析小包 — 非完整金庫

- 產生時間：2026-11-19T14:31:27+08:00
- 來源 run：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1/runs/20261119T132017_bd1972b2`
- 方針：先修分析口徑 → 進場品質／集中風險／止損執行；**不**因這段虧損直接改參數

## 1) PIPPIN 13:54～14:01（超額止損）

目錄：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/pack_20261119_pippin_tao_fet_lit_jup/01_PIPPIN_1354_1401`

| 檔案 | 說明 |
|------|------|
| fills_PIPPINUSDT.jsonl | OPEN 13:55 / CLOSE INITIAL_STOP 13:59（淨約 -179.8） |
| quote_events_PIPPINUSDT.jsonl | 視窗內報價（n=55） |
| stop_events_PIPPINUSDT.jsonl | 止損事件（n=0） |
| position_events_PIPPINUSDT.jsonl | 持倉事件（n=2） |
| decision_trace_PIPPINUSDT.jsonl | 進場前後決策 |
| PIPPINUSDT_1m_1350_1405.csv | 切片 K |
| PIPPINUSDT_1m_full_available.csv | run 內完整可用 1m |

已知：`INITIAL_STOP` 平倉價 0.017283 vs 進場 0.016987（空？需對 side；填單顯示開倉價低於平倉且虧損大 → 先核對多空與滑價）。

## 2) 14:03 批次 TAO／FET + BTC 結構（13:30～14:20）

目錄：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/pack_20261119_pippin_tao_fet_lit_jup/02_batch_1403_TAO_FET_BTC`

| 檔案 | 說明 |
|------|------|
| fills_TAO_FET.jsonl | 14:03 同秒開倉；約 14:05 INITIAL_STOP |
| stop_events_TAO_FET.jsonl | 止損 |
| quote_events_TAO_FET_1400_1410.jsonl | 報價 |
| decision_trace_TAO_FET_1400_1410.jsonl | 決策 |
| TAOUSDT_1m_1330_1420.csv | 結構窗 |
| FETUSDT_1m_1330_1420.csv | 結構窗 |
| BTCUSDT_1m_1330_1420.csv | 大盤對照 |
| *_1m_full_available.csv | 完整可用（暖機不足再往 vault 補） |

## 3) LIT（成功）vs JUP（成功軌跡不同／可對照）

目錄：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/pack_20261119_pippin_tao_fet_lit_jup/03_LIT_vs_JUP`

| 檔案 | 說明 |
|------|------|
| fills_*.jsonl | LIT：開倉→部分止盈→移動停利賺；JUP：開倉→部分止盈→移動停利賺 |
| stop/position/decision_* | 對照 |
| *_1m_1245_1420.csv | 切片 |
| *_1m_full_available.csv | 完整 |

註：root fills 另有更早一筆 TAO（12:47）屬前次 run 延續帳戶；本包以 **本 run fills** 為主。

## 原始完整路徑（未整包複製）

- quote_events：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1/runs/20261119T132017_bd1972b2/quote_events.jsonl`
- stop_events：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1/runs/20261119T132017_bd1972b2/stop_events.jsonl`
- klines_1m：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_bulk_test_v1/runs/20261119T132017_bd1972b2/klines_1m/`
- vault：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/data/paper_bulk_test_v1/kline_vault_v2_clean/`
