# 反向 paper 啟動回報（僅模擬）

## 狀態分層
- **啟動成功**：是（PID 存活；已過暖機、掃描、盯倉）
- **設定驗證**：是（config_snapshot + 離線測試）
- **首次成交驗證**：是（ETHFIUSDT；方向翻轉與 TP/SL 幾何通過）
- **策略驗收成功**：否（一日測試啟動 ≠ 績效驗收）

## 本金／帳戶／路徑
- 本金：**50000 USDT**（注入不算盈利）
- profile：`paper_reverse_fixed_exit_v1`
- run_id：`20261119T204955_8327b8b5`
- 輸出：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fixed_data_root/output/paper_reverse_fixed_exit_v1`
- sealed：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/fix_source_sealed_20261119T204210`
- runtime：`/Users/alan/MyProjects/automax/output/paper_bulk_test_v1_setup/runtime_rev_fixed_exit_20261119T204210`
- tag：`fix_20261119_reverse_tp1_sl15_dl8000`
- config_hash：`0585b1b4036d4a6a`

## 生效參數
- 訊號：原方向翻轉（首次：orig LONG → SHORT；成交價按新方向重建）
- SL=1.5×入場 ATR；TP=1.0×入場 ATR；ATR 入場固定
- 關閉 BE／partial／trail／反手重開
- 日損 8000（日曆午夜切日）；單筆風險 100；名義 10000；保證金 1000（不隨本金放大）
- 最多 20 倉／每標的 1／同批同向 1
- 新開倉截止：2026-11-20T20:15:00+08:00（之後只管理）

## 首次成交核對
- symbol=ETHFIUSDT side=SHORT orig_side=LONG flip=True
- entry=0.51919614 atr=0.0014075872404355322
- SL=0.5213075208606534 TP=0.5177885527595645
- mults stop=1.5 tp=1.0
- fee=4.999988627235001 notional≈9999.97725447 margin=1000.0

## 時間
- 穩定啟動：2026-11-19T20:49:55+08:00
- 觀測結算界：2026-11-20T20:15:00+08:00
- 報告目標：2026-11-20T20:25:00+08:00
- 現在：2026-11-19T20:54:33.867342+08:00
- 至結算界約 23.4 小時
- 日損區間：2026-11-19（啟動→午夜）、2026-11-20（午夜→20:15）；8000≠整場累計上限

## 舊帳戶
- paper_bulk_test_v1 唯讀；FETUSDT LONG + 1000PEPEUSDT SHORT 未搬移未平倉
- 舊帳戶 hash 未變；舊 bulk 程序未運行

## 報告排程
- launchd：**未載入** — 不保證明天自動出報告
- 手動（結算後）可對本帳戶自行彙總 fills／account_state；現有 `paper_daily_review` 預設綁定舊 bulk profile，產反向報告前需擴充或指定路徑（待批）

## 阻擋／注意
- 前兩次啟動暖機中退出；第三次持久前景啟動成功
- 另有無關 breakout_follow_v1 paper 進程，未共寫本帳戶
