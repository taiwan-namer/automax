# Round-5：A／B／C 逐筆時間序列稽核

**快照截止（全體共用）**：`2026-11-20T03:01:00+08:00`  
唯讀：未改策略／未重啟／未下單／未強制平倉。

## 一、時間與策略身份

| 帳戶 | 開始 | 規則 | 路徑 |
|------|------|------|------|
| **BULK（原≈−3600）** | 12:47→最後成交 **20:07** | 自掃（非 A/B/C） | `paper_bulk_test_v1`｜已實現≈**−3614.7**（凍結） |
| **A** | 首筆 **20:53** | **A1** 自掃+flip+ATR1/1.5 → **A2** 自 **01:04~01:07** 鏡像B同向+ATR | `paper_reverse_fixed_exit_v1` |
| **B** | **23:25** | 自掃+flip+固定毛+50/−75 | `paper_reverse_fixed_usdt_50_75_v1` |
| **C** | **00:54**（01:07重載） | 鏡像B反向+固定毛+50/−75 | `paper_reverse_fixed_usdt_50_75_inv_v1` |

「八點到現在」：**12:47–20:07=BULK**；其後才是 A→B→C。不可混算。PID 33829≠上述帳戶。

## 四、R3／R4 口徑更正（同樣本 n=54）

| 口徑 | 參考價 | 公式（>0=對實際方向不利） | 均值 |
|------|--------|---------------------------|------|
| R3 | bar **OPEN** quote mid | LONG:(entry_mid−ref)×qty | **-39.0** |
| R4 | **S.close** | LONG:(entry−S.close)×qty | **9.6** |

**更正**：R3≈−39U 含突破棒內部位移，在 flip 下常對翻轉方向「有利」，**不能**解釋為延遲無傷害。延遲應看 R4（≈+9.6U）。舊解讀作廢。

## 五、Round-4 共 63 筆互斥分類

|分類|n|
|---|---|
| B嚴格支持 | 23 |
| B反駁 | 15 |
| 止盈未達反駁門檻 | 10 |
| 停損無早期不利證據 | 7 |
| 未平倉 | 5 |
| B弱支持 | 2 |
| 資料不足 | 1 |

### 其餘 23 筆

|trade_id|幣|分類|退出|淨|原因|
|---|---|---|---|---|---|
| `20261119T232447_2056c35e:SKYUSDT:5388b399a5` | SKYUSDT | DATA_INSUFFICIENT | FIXED_TP | 74.4 | 持倉 quotes=1<3，不足判定續走/反轉 |
| `20261119T232447_2056c35e:VVVUSDT:655648fd17` | VVVUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 42.6 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:SKYUSDT:69d47cee88` | SKYUSDT | OTHER_SL_WITHOUT_EARLY_ADVERSE_EVIDENCE | INITIAL_STOP | -100.7 | 停損但早期時點未錄到明確不利/續走（可能瞬間穿價或時點落在 gap） |
| `20261119T232447_2056c35e:SKYUSDT:e2dad7ebe6` | SKYUSDT | OTHER_SL_WITHOUT_EARLY_ADVERSE_EVIDENCE | INITIAL_STOP | -127.7 | 停損但早期時點未錄到明確不利/續走（可能瞬間穿價或時點落在 gap） |
| `20261119T232447_2056c35e:ARBUSDT:1f8195dafc` | ARBUSDT | OTHER_SL_WITHOUT_EARLY_ADVERSE_EVIDENCE | INITIAL_STOP | -94.2 | 停損但早期時點未錄到明確不利/續走（可能瞬間穿價或時點落在 gap） |
| `20261119T232447_2056c35e:STRKUSDT:430bc99848` | STRKUSDT | OTHER_SL_WITHOUT_EARLY_ADVERSE_EVIDENCE | INITIAL_STOP | -87.1 | 停損但早期時點未錄到明確不利/續走（可能瞬間穿價或時點落在 gap） |
| `20261119T232447_2056c35e:FILUSDT:6ff5812eea` | FILUSDT | OTHER_SL_WITHOUT_EARLY_ADVERSE_EVIDENCE | INITIAL_STOP | -85.1 | 停損但早期時點未錄到明確不利/續走（可能瞬間穿價或時點落在 gap） |
| `20261119T232447_2056c35e:MEGAUSDT:8388e9b01a` | MEGAUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 47.2 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:BCHUSDT:c56a1ed1cb` | BCHUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 42.6 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:1000PEPEUSDT:cf0df99ce1` | 1000PEPEUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 40.9 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:BCHUSDT:0e0c398356` | BCHUSDT | OTHER_SL_WITHOUT_EARLY_ADVERSE_EVIDENCE | INITIAL_STOP | -85.5 | 停損但早期時點未錄到明確不利/續走（可能瞬間穿價或時點落在 gap） |
| `20261119T232447_2056c35e:ETHUSDT:1965f03d81` | ETHUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 41.1 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:KAVAUSDT:30ee8f111d` | KAVAUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 41.8 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:ZKCUSDT:c7aeb0adfc` | ZKCUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 40.2 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:BREVUSDT:b9c63a34b4` | BREVUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 40.4 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:SENTUSDT:bb1947e30e` | SENTUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 48.0 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:INITUSDT:72ed9c02c8` | INITUSDT | OTHER_SL_WITHOUT_EARLY_ADVERSE_EVIDENCE | INITIAL_STOP | -85.4 | 停損但早期時點未錄到明確不利/續走（可能瞬間穿價或時點落在 gap） |
| `20261119T232447_2056c35e:TAOUSDT:f72ed1fd22` | TAOUSDT | OTHER_TP_WITHOUT_EARLY_FAV_FLAG | FIXED_TP | 41.1 | 止盈但未達反駁門檻（早期有利時點不足或 MFE 門檻） |
| `20261119T232447_2056c35e:BMTUSDT:9565060036` | BMTUSDT | OPEN_AT_R4_CUTOFF |  |  | 未平倉，不計支持/反駁 |
| `20261119T232447_2056c35e:BNBUSDT:83facfadf3` | BNBUSDT | OPEN_AT_R4_CUTOFF |  |  | 未平倉，不計支持/反駁 |
| `20261119T232447_2056c35e:KAVAUSDT:83442fe8ba` | KAVAUSDT | OPEN_AT_R4_CUTOFF |  |  | 未平倉，不計支持/反駁 |
| `20261119T232447_2056c35e:CAPUSDT:296e7a6116` | CAPUSDT | OPEN_AT_R4_CUTOFF |  |  | 未平倉，不計支持/反駁 |
| `20261119T232447_2056c35e:DOGEUSDT:b27ff700f1` | DOGEUSDT | OPEN_AT_R4_CUTOFF |  |  | 未平倉，不計支持/反駁 |

## 六、根因判讀

### A

- 毛 205.7（扣費前已非負）；費 899.6；淨 -694.0
- TP 49／SL 41；SL超額合計 214.7
- 有虧小時 5／有盈小時 2；連敗最長 4
- 去最大5虧後淨仍 **-139.5**（5虧占虧損側 26.3%）→ **持續虧損為主**
- 先浮盈再停損（MFE>10&MAE<-50&SL）：2；逆突破且停損：24
- 鏡像延遲 n=35 均 52.1s
- 虧幣 top：[('TAOUSDT', 3), ('BCHUSDT', 3), ('APTUSDT', 3), ('DOGEUSDT', 3), ('KAVAUSDT', 2), ('BTCUSDT', 2), ('ETHFIUSDT', 2), ('AAVEUSDT', 2)]
- 序列：`LWLWLWWLWLLWLLWLLWLWWWWWLWLLWLWLWWWWWLWLWLWLLWLWLWLWWLWWWWWLLWWLLWWLWLWWLWLLWWWLLLLWLLLLWW`

### B

- 毛 -1238.1（扣費前已負）；費 600.1；淨 -1838.2
- TP 27／SL 33；SL超額合計 314.1
- 有虧小時 4／有盈小時 0；連敗最長 5
- 去最大5虧後淨仍 **-1231.4**（5虧占虧損側 19.5%）→ **持續虧損為主**
- 先浮盈再停損（MFE>10&MAE<-50&SL）：19；逆突破且停損：33
- 虧幣 top：[('IRYSUSDT', 2), ('SOLUSDT', 2), ('SKYUSDT', 2), ('ARBUSDT', 2), ('NAORISUSDT', 2), ('VVVUSDT', 1), ('OPUSDT', 1), ('TAOUSDT', 1)]
- 序列：`LLLLLWWLLLWLWLWLLWLLLLLWLLLWWWWWWWWLLLWWLLWWWLWWLWWLWLLLLWLW`

### C

- 毛 -1338.4（扣費前已負）；費 350.1；淨 -1688.5
- TP 12／SL 23；SL超額合計 265.5
- 有虧小時 3／有盈小時 1；連敗最長 8
- 去最大5虧後淨仍 **-1040.4**（5虧占虧損側 29.2%）→ **持續虧損為主**
- 先浮盈再停損（MFE>10&MAE<-50&SL）：13；逆突破且停損：23
- 鏡像延遲 n=41 均 55.6s
- 虧幣 top：[('MEGAUSDT', 1), ('BCHUSDT', 1), ('FETUSDT', 1), ('QNTUSDT', 1), ('LINKUSDT', 1), ('1000PEPEUSDT', 1), ('INITUSDT', 1), ('UNIUSDT', 1)]
- 序列：`WWWLLLLLLLLWLLWLLLWLWLLLLWLLWLWWWLL`

並存機制：方向/逆勢結構；固定退出+SL超額；S.close→成交延遲變位；先盈後掃；鏡像滯後。勝率是結果不是根因。

完整逐筆見下列 REPORT_*.md 與 CSV。
