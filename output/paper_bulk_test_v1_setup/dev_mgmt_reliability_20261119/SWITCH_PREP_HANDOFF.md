# 切換準備（未執行切換）

## 離線承接驗證
- 活帳戶讀取前後 sha256 一致：`True`
- 持倉數：2
- schema 相容：`True`
- 日損鎖（讀取當下）：`False`（未手動修改）
- 重載持倉冪等：`True`

### 持倉快照（讀取）
- ENAUSDT SHORT `20261119T132017_bd1972b2:ENAUSDT:cd1f2725f2` qty=89704.0 eff_stop=0.1118088515012346 partial=False fees=4.999997800399999
- LITUSDT SHORT `20261119T132017_bd1972b2:LITUSDT:67e371c0b1` qty=3422.4 eff_stop=2.9341673306174507 partial=False fees=4.9999818036

## 停止行為
- hours到期或進程終止：engine 明示持倉維持不強制平倉；無獨立 manage-only CLI
- 午夜是摘要截止，不是自動停掉持倉管理。

## 不可原地 manage-only
- 舊程序無獨立安全停新倉開關；不可聲稱原地切 manage-only

## 建議單實例切換步驟（待批准；本輪不執行）
1. 確認僅一個交易 PID；準備新 sealed runtime（本 DEV 另封）。
2. 停止舊程序並確認進程退出（不假設自動平倉）。
3. 記錄停止→新版啟動的管理空窗（不宣稱零空窗）。
4. 以同一 `account_state.json` 啟動新版單實例（不 `--once` 當初始化）。
5. 驗證無重複 OPEN/PARTIAL/CLOSE；持倉欄位保留。
6. 回滾：切回舊 binary／runtime，**不得**還原舊帳戶快照。

## 部署阻擋（目前）
- 本 DEV 尚未封裝為新 sealed runtime
- 未批准部署／切換
- 舊程序仍有持倉且持續開倉
- 無已批准的安全停新倉開關
