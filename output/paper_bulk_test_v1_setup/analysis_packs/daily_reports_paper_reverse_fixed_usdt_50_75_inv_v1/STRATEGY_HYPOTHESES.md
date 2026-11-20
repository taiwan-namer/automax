# 策略假設追蹤（跨日）

狀態：open / supporting / weak / rejected / pending_approval
規則：承接上一日；不每天重頭猜；執行可靠性 vs 進場策略分開。

- [weak] CAND_COST_THRESHOLD: 成本門檻／進場與目標相容性 — 提案 only，未部署
- [weak] CAND_BATCH_SAME_DIR: 同批同向集中曝險限制 — 提案 only，未部署
- [weak] CAND_BREAKOUT_QUALITY: 突破品質濾網（完整樣本） — 提案 only，未部署
