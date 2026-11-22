# COUNTERFACTUAL_AUDIT — Stage 2

## Verdict
PARTIAL_VALID_FOR_DIRECTIONAL_COMPARE

## B0
- 帳務重現 ≠ 模擬引擎路徑重現
- B0 nets rebuild layer PnL from recorded OPEN/ADD/CLOSE fills; does not re-run manage_c_pyramid quote loop

## B1/B2/B3
- Exit prices primarily from baseline fills on the shared P0 timeline (observed marks/limits).
- B2 protect-at-P2: trigger time = baseline `PYRAMID_ADD` P2 `recorded_at`; fill = that ADD price (observed), not a lone theoretical P2 printed as fill.
- Theoretical P2 level is recorded for comparison; **not** asserted as independently observed executable without the ADD print.
- Slippage: embedded in baseline mark exits (`SLIPPAGE_ADVERSE=0.0002`); B2 protect reuses limit ADD price without extra slip.
- No future close backfill; cutoff opens remain OPEN/UNKNOWN unrealized where layers diverge.

## Limits
- B0 is accounting reproduce from fills, not engine path reproduce
- B1/B2/B3 exits mostly remap baseline observed exit marks; not independent quote-driven simulation
- B2 protect uses observed P2 ADD fill price as trigger+fill proxy — not theory-only threshold as fill, not future close backfill
- No invention of fills after archive stop; open cycles stay OPEN
- 1m OHLC not used as intrabar fill oracle in this paired model
- Cannot claim maker fills; fee stage-1 uniform 0.0005

## B2 split check
- common14 net ≈ -13.918352 (expect ~-13.92) ok=True
- extra2 net ≈ -7.011945 (expect ~-7.01) ok=True

Detail CSV: `counterfactual_exit_audit.csv`