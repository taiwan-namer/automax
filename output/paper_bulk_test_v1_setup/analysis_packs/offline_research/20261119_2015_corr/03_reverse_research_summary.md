# 3. 反向研究規則／可行性摘要

## 資料是否足夠？
- **足夠定義規則並做「既有入場集合」條件診斷**（n=106 有 CLOSE；其中 atr_at_open 可用≈106）。
- **不足宣稱完整策略回測**：無完整拒絕宇宙重放；動態 trail/BE/partial 無唯一 OHLC 鏡像；funding 缺口。
- 缺K／衝突樣本：**不得靜默剔除**（CSV 已標記）。

## A. 算術對照（已算，非可交易）
- 規則：退出毛損益翻號，費用保留為成本 → `arith_flip_net = -exit_gross - fees` 等待，本實作 `flipped_gross - fees`。
- 原 sum_net≈-3604.6890；算術翻號後 sum≈1484.3651
- 標籤：**NON_TRADEABLE_ARITHMETIC_FLIP**
- 檔：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/analysis_packs/offline_research/20261119_2015_corr/03_reverse_A_arithmetic.csv`

## B. 可執行規則（已凍結定義，本輪不搜參、不執行路徑回放）
### B1 — 訊號反向＋保留原風控架構
{
  "name": "訊號反向＋保留原風控架構",
  "entry": "同一突破訊號，方向取反（原 LONG→SHORT，原 SHORT→LONG）；進場價改對側 book 不利價（原模型：LONG 用 ask、SHORT 用 bid；反向後對調）。",
  "stops": "初始止損距離以原 ATR×1.5 對稱置於反向部位；BE／部分止盈／追蹤止損規則與原 portfolio.manage_position 相同（參數不變）。",
  "ambiguity": [
    "原 trail／partial 依有利方向極值推進；反向後極值定義對調，但路徑依賴於輪詢報價，1m OHLC 無法唯一重放。",
    "反手（reverse）訊號在原策略為『反向突破則平舊開新』；方案B1若只翻新開倉集合，不含原反手狀態機。"
  ],
  "data_sufficiency": "僅能在『既有成交入場集合』上做條件診斷（同一 signal_key／時間），不能宣稱完整宇宙策略回測。",
  "ohlc_ambiguity_policy": "同1m棒同時觸及止盈與止損時，採保守（對該方案較不利）結果並計入歧義筆數；不捏造 book 路徑。",
  "status": "RULES_DEFINED_NOT_EXECUTED_PENDING_CONFIRM"
}

### B2 — 方向反向＋短止盈／寬止損（固定）
{
  "name": "方向反向＋較短止盈／較寬止損（固定、不搜參）",
  "entry": "同 B1 的訊號反向進場。",
  "stops": "固定：止損 = 2.0×ATR（較寬）；止盈 = 0.75×ATR（較短）；不啟用原 trail／BE／partial（因無法唯一鏡像原動態結構）。",
  "ambiguity": [
    "關閉 trail/BE/partial 是為消除鏡像歧義的明確選擇，不是聲稱最優。",
    "與原策略風控架構不同，結果不可直接與原策略績效因果對比。"
  ],
  "data_sufficiency": "同樣限於既有入場集合＋1m OHLC 保守觸價；標記為條件診斷。",
  "ohlc_ambiguity_policy": "同棒雙觸採保守；缺K／衝突K列為不可靜默剔除樣本。",
  "status": "RULES_DEFINED_NOT_EXECUTED_PENDING_CONFIRM"
}

## 歧義與下一步
- 原策略動態追蹤／部分止盈 **無法唯一鏡像** → B1 保留架構但 OHLC 回放必報保守雙觸歧義；B2 明確關掉動態模組以消歧。
- 當天為**探索樣本**；方案凍結後須用未參與設計的後續資料驗證，**不可把當日結果當樣外成效**。
- 狀態：規則已寫明；**完整 OHLC 路徑回放待你確認後另跑**（本輪先交付可行性，避免未確認假設下產出偽回測數字）。
