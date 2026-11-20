# 1. 帳目與報告修正摘要

產出時間：2026-11-19T20:25:59.728747+08:00
資料截止：asof≈2026-11-19T20:18:12+08:00（報告原值）；paper **未運行**（無 pid）；本目錄唯讀分析，未改帳戶／sealed／金庫。

## 日界結束 vs 資料覆蓋
| 項目 | 值 |
|------|-----|
| 結算視窗 | `2026-11-18T20:15:00+08:00` ≤ t < `2026-11-19T20:15:00+08:00` |
| 日界是否已結束 | **是**（asof 20:18 > 20:15） |
| 實際成交覆蓋 | first `2026-11-19T12:47:07+08:00` → last close-in-window / open `2026-11-19T20:07:13+08:00` |
| 覆蓋缺口 | 視窗前段 **2026-11-18T20:15 → 2026-11-19T12:47** 無成交（策略當日約 12:47 才開始有 fill） |
| 起始結算快照 | 最近 equity `2026-11-19T12:38:41`，相對 20:15 起點 **lag≈59021s** → settlement_snapshot_ok=**False** |
| 結束結算快照 | `2026-11-19T20:15:13` equity≈46371.41，相對 20:15 **+13s** |
| 20:15–20:15:13 成交 | **無** fills；該 13s 僅估值／浮動變化（結束快照 floating≈-13.9） |
| 完整性標籤修正 | 原「完整日」=日界已過+JSONL未截斷；**應同時標「資料覆蓋不完整（無日界起點快照；成交未覆蓋全窗）」** |

## 106 筆帳目核對
- root+runs fills 去重後：OPEN=108, CLOSE=106, PARTIAL=10；完整且 CLOSE∈窗：**106**；未平倉：**2**
- 去重移除：224（root 與 run 鏡像）
- 數量閉合失敗：0；多 OPEN：0
- C 合計 net≈-3604.689008；fees_total≈1060.161954
- 開倉費計入 complete_net（realized_net_u 若存在含 0；否則 −fee_u）；退出用 net_u；**不**另扣滑價（已含成交價模型）
- **funding_data_gap=True**（全程）

## 106 vs 累計 50
報告 §7 累計 n=50／sum≈-1757 來自重跑當下讀到的「舊 metrics.json」（同日稍早部分日產出）。hist 在覆寫 metrics 之前累加舊檔，故與本次 C=106 不一致；DAILY_INDEX 現已是 106 日結果。正確累計（僅有報表之日）應為 n=106／sum_net≈-3604.69。

## 部署狀態修正（依 sealed／切換驗收）
| 項目 | 報告原文 | 修正 |
|------|----------|------|
| 管理可靠性 | 設計／未部署 | **已部署**（sealed `155304` → 運行；C 沿用 manage_pass） |
| 同批同向限制 | 候選未部署 | **已部署**（sealed `160723`／tag `fix_20261119_mgmt_dl5000_batchside1`） |
| 日損 5000 | 設定 5000 | **已部署**（C） |
| 跨 run | 有列 | 維持；另分 **跨策略版本**（A/B/C bucket） |
| launchd | 未載入 | **核對確認未載入**（`launchctl print` 無此服務；無 LaunchAgents plist） |

## 管理完成間隔（按版本）
{
  "B_mgmt_reliability": {
    "n_complete_events": 112,
    "n_gaps": 111,
    "mean_gap": 4.90990990990991,
    "max_gap": 6.0,
    "n_expected0": 0,
    "coverage": "has_MANAGE_BATCH_COMPLETE"
  },
  "C_dl5000_batchside1": {
    "n_complete_events": 2926,
    "n_gaps": 2925,
    "mean_gap": 4.936410256410256,
    "max_gap": 16.0,
    "n_expected0": 0,
    "coverage": "has_MANAGE_BATCH_COMPLETE"
  },
  "A_pre_mgmt_dl2000": {
    "n_complete_events": 0,
    "coverage": "NO_complete_events_in_window",
    "status": "NA"
  }
}

- A 早期：**無** `MANAGE_BATCH_COMPLETE` → 完成間隔 **NA**（不可用 quote 前置冒充）
- B/C：有完成事件；報告 mean≈5.06s 來自完成事件，但 **max 含空倉／切換空窗**，需分 expected_n 與持倉態閱讀
- quote 前置批次間隔（max≈374s／396s）= **pre-manage quote**，≠ 完成間隔

## 排程
launchd **未載入**（只讀核對）；未自動安裝。
