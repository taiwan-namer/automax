"""Per decision row: live (archive-bar) ATR / vol_x versus post-hoc complete-bar ATR / vol_x."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import analyze as A

arch = A.load_bars(A.PACK / "klines_1m")
truth = A.load_bars(A.POSTHOC)
ra, rv, partial_prior = [], [], []
with open(A.PACK / "signals/decision_trace.jsonl") as f:
    for line in f:
        d = json.loads(line)
        s = d["symbol"]
        S = int(d["vol_metric"]["numerator_open_time"]) if d.get("vol_metric") else None
        if S is None or s not in truth:
            continue
        a = A.signal_on(arch[s], S)
        t = A.signal_on(truth[s], S)
        if not a or not t or not t["atr"] or not t["vol_x"]:
            continue
        ra.append(a["atr"] / t["atr"])
        rv.append(a["vol_x"] / t["vol_x"])
        b, tb = arch[s], truth[s]
        i = b.idx[S]
        n = 0
        for k in range(max(0, i - 20), i):
            j = tb.idx.get(int(b.ot[k]))
            if j is not None and abs(b.v[k] - tb.v[j]) > 1e-12:
                n += 1
        partial_prior.append(n)
ra, rv, pp = np.array(ra), np.array(rv), np.array(partial_prior)
res = {
    "n": int(len(ra)),
    "atr_ratio_live_over_posthoc": {q: float(np.percentile(ra, q)) for q in (10, 25, 50, 75, 90)},
    "volx_ratio_live_over_posthoc": {q: float(np.percentile(rv, q)) for q in (10, 25, 50, 75, 90)},
    "share_atr_live_lower": float((ra < 0.999).mean()),
    "share_volx_live_higher": float((rv > 1.001).mean()),
    "prior20_bars_differing_median": float(np.median(pp)),
    "prior20_bars_differing_mean": float(pp.mean()),
}
Path(__file__).parent.joinpath("out/atr_volx_ratio.json").write_text(json.dumps(res, indent=1))
print(json.dumps(res, indent=1))
