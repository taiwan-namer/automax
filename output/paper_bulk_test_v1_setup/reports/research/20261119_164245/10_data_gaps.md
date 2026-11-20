# 10 資料缺口

1. **舊版無 `scan_batch_id`／`SCAN_BATCH_RANK`**  
   - 影響：無法對 A/B 重放「同批同向」決策邊界  
   - 可否恢復：不能可靠假造當時批次  
   - 未來紀錄：已在 C 部署；無需本輪改碼  
   - 外部抓取：不需要

2. **A 早期缺少 `MANAGE_BATCH_COMPLETE` 完成事件**  
   - 影響：管理完成間隔、部分成功語義不可比  
   - 恢復：不可用 quote 時間冒充  
   - 未來：B/C 已有；歷史缺口保留標記

3. **trail／BE／effective_stop 變更路徑未完整落在獨立事件流**  
   - 影響：不能由最終持倉倒推完整 stop 演進  
   - 現有：OPEN 含 initial_stop；部分在 manage 結果／帳戶欄位  
   - 未來最小變更（**僅建議，本輪不部署**）：每次 stop 變更寫 position_event（old→new, reason, ts, trade_id）

4. **decision 未保證「全宇宙每個標的每分鐘都有列」**  
   - 影響：不能把 SIGNAL_EVAL 集合當成完整標的池快照  
   - 恢復：不可事後補齊未記錄掃描  
   - 建議：若需研究拒因覆蓋率，未來可選記 universe_snapshot_id（建議項）

5. **無聯網補歷史深度 K線**  
   - 影響：更長暖機／跨日 HTF 研究可能不足  
   - 需另行批准外部抓取

6. **部分日**  
   - 影響：勝率／期望不可外推全日；午夜報告另開

7. **開倉時 `scan_batch_id` 回填**  
   - 部分早期 OPEN 的 scan_batch_id 為空（版本無此欄）  
   - 不冒充

truncated_tails: []
