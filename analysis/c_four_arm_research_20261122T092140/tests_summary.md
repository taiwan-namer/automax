# tests_summary — Stage 2

## 執行紀錄

```text
python -m unittest analysis.c_four_arm_research_20261122T092140.tests.test_g7_g8_g9 -v
→ Ran 13 tests … OK

python -m unittest analysis.c_four_arm_research_20261122T092140.tests.test_c_research_gates -v
→ Ran 6 tests … OK
```

## G7

| 測試 | 結果 |
|------|------|
| enrich 必填欄位 + paper_simulation | PASS |
| tick 驗證、拒 0.2% 寬鬆 | PASS |

## G8

| 測試 | 結果 |
|------|------|
| save 失敗 raise + flag，擋新曝險 | PASS |
| pyramid ADD 被 LEDGER_WRITE_FAILED 擋 | PASS |
| 重啟載入 flag 後仍擋 | PASS |

## G9

| 案例 | 結果 |
|------|------|
| 同報價加倉+停損 → 不停加倉 | PASS |
| 跳過多門檻 → stop 優先、無 ADD | PASS |
| B2 觸 P2 → protect、不加 P2 | PASS |
| P1 R2 後不重掛；P2 仍 PENDING | PASS |
| ADD 不覆寫 P0 | PASS |
| 撤單請求後晚到量欄位可持久化 | PASS |
| 重啟恢復 ledger 旗標 | PASS |
| 舊/新 cycle 隔離 | PASS |
| 重複訊號 | PASS |
| 交易所硬禁用 | PASS |
| 退出×晚到成交完整 apply | 薄（僅持久化／既有 digest 路徑） |

## Paper

`acceptance_gate.json` → **DO_NOT_LAUNCH_PAPER**
