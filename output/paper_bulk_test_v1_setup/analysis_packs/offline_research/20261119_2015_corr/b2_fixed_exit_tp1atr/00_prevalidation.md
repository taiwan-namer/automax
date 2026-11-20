# 資料前置驗證（TP=1ATR / SL=2ATR 基準）

## Paper 停止證據（只讀）
- paper.pid: False
- paper_bulk paper_main procs: 0
- last_manage: 2026-11-19T20:17:59+08:00 status=OK
- OPEN FETUSDT LONG tid=20261119T161354_5cb8369a:FETUSDT:1bd4e0c0cc entry_time=2026-11-19T19:33:15+08:00 eff_stop=0.1291590154681085 trail=None
- OPEN 1000PEPEUSDT SHORT tid=20261119T161354_5cb8369a:1000PEPEUSDT:f30831260d entry_time=2026-11-19T20:07:13+08:00 eff_stop=0.003927802200094158 trail=None

## 本輪參數
- TP = **1.0 × 入場時 ATR**；SL = **2.0 × 入場時 ATR**；ATR 固定入場值
- 關閉 BE / partial / trail / 反手；不跑 0.75 ATR；不搜索參數
- 原方向與反方向皆用相同固定退出

## 特徵／公式／K線（沿用）
- vol_x/spread/atr/bid/ask：決策當時紀錄；post_ret 離線重算且不填補未來
- K線疑點 unclosed=38；dup_ot=0；fetched_before_close=0
- opens=108 missing=0 hard=[]
- 閘門：通過則進入回放；未通過不硬產反向收益
