# D 低波動小幣做空 — 建置報告（未啟動）

- 建置時間：2026-11-20T13:37:49+08:00（Asia/Taipei）
- 版本：`d_quiet_short_v1_20261120`
- Profile：`paper_d_quiet_short_v1`
- **狀態：READY_FOR_OTHER_MACHINE_PAPER_FILLS**（本機仍不啟動；不動 A／B／C）
- 資金／深度：2026-11-20 **已確認**
- A／B／C：未修改、未重啟

## 1. 新增／更新（摘要）
- `config.py`：確認保證金 300×10、毛額 TP／SL、雙側近價深度
- `market.py`：0.1% 近價 bid／ask 加總 + 3000U walk
- `engine.py`：進場深度不足阻擋；退出 walk ask 並揭露 shortfall
- 驗收與 `START_ON_OTHER_MACHINE.md`

## 2. 最終配置（已確認）
| 項 | 設定 |
|---|---|
| 300 U | **保證金** |
| 槓桿 | **10×** → 名義 **3000 U** |
| TP／SL | **毛額** 150／30；報告另列費／滑／資金費→淨利 |
| 30 U | 觸發門檻，非最大虧損保證 |
| 價差 | `0.0008` = **0.08%** of mid |
| 成交量 | `20M` = **24h USDT quoteVolume** |
| 深度 | 最佳價各 **0.1%** 內，bid／ask **各 ≥50k**；記錄 3000U walk |
| 退出 | 深度不足仍平倉並揭露 shortfall |
| TTL | 無時間 TTL；`mid≤P×0.99` 撤單 |

**本機：不啟動。換機：可用 `--allow-fills`。**

## 3. 排除與成交模型
- 硬排除：BTCUSDT、ETHUSDT
- 另排除：TradFi／股票／指數／穩定幣／槓桿代幣（專案清單）
- **未宣稱完整排除全部大型幣**
- 進場成交：`bid≥限價`→以限價記成交（餘量模型；非保證全量）
- 平倉：ask×(1+0.0002)；不用 mid
- 費率 5bps／邊；無 maker 優惠；模擬≠實盤

## 4. 驗收
`ALL D ACCEPTANCE PASSED`（15m 完整K、P=10掛撤、無TTL、資格撤單、名額20、同幣不重複、TP／SL／波動、重建4根、過期報價、阻擋未確認成交、排序、成本模型）

## 5. 狀態
- **未啟動**（本機依要求不跑）
- 預設僅掃描；`--allow-fills` 在未確認時拒絕

## 6. 路徑
- Runtime：`…/runtime_d_quiet_short_20261120T133749/`
- 未來資料：`{runtime-root}/output/paper_d_quiet_short_v1/`（fills／events／signals／settlement／review）
- 換機說明：`START_ON_OTHER_MACHINE.md`

## 7. 回滾
刪 D runtime 或 D 輸出目錄即可；勿動 A／B／C。

## 8. 帳戶回撤
未計算（標註 NOT_COMPUTED）。
