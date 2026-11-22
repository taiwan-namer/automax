# COUNTERFACTUAL_CREDIBILITY_V2

## Per-arm grades (no single Verdict)
- **B0**: `ACCOUNTING_REPRODUCE` — historical fills reuse; not engine-path reproduce
- **B1**: `COUNTERFACTUAL_REMAP_OBSERVED_EXITS` — management remap on baseline exit marks
- **B2**: `COUNTERFACTUAL_ADD_PRICE_PROXY_PLUS_OPTIONAL_BIDASK` — protect exit originally used opposite-side ADD limit as price proxy; v2 keeps proxy labeled; bid/ask+slip scenario separate; proxy trigger time ≠ proven independent first touch; FORBIDDEN to claim B2 winner from these historical numbers
- **B3**: `COUNTERFACTUAL_REMAP_OBSERVED_EXITS` — P0-only remap on baseline P0 exits/stops

## B2 price proxy
- Original protect fill = baseline P2 ADD limit → `counterfactual_add_price_proxy`.
- That ADD is paper fill evidence, not exchange evidence, and is opposite liquidity side vs protect exit.
- Where contemporaneous bid/ask exist (±5s), LONG exit = bid×(1−slip), SHORT = ask×(1+slip).
- ADD limit had no exit slip embedded → slip applied once on bid/ask path.
- Proxy trigger time is baseline ADD time only — not proven equal to independent B2 first touch.
- **Do not claim B2 wins from historical paired numbers.**

- proxy extra2 net ≈ -7.011945
- bidask+slip extra2 net ≈ -7.618976