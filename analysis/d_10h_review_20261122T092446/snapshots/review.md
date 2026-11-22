# D quiet-short review stub

- version: `d_quiet_short_testnet_20261121`
- updated: 2026-11-21T20:15:33.839050+08:00
- paper fills allowed: `True`

## Pending confirmations
```json
[]
```

## Account-level drawdown
未完成完整帳戶權益序列回撤驗證（標註）。

## Fill model
- entry: limit touch when bid≥limit; fill at limit (remainder model; not guaranteed by depth)
- exit: aggressive cover at ask×(1+slip); mid never used as fill
- fees: 0.0005 one side; slip 0.0002; no maker rebate
