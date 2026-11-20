"""
Counterfactual 1m-bar simulator for breakout_follow_v1 (offline research only).

Entry: uses the RECORDED scan-time bookTicker (quote_events SIGNAL_EVAL) for the same
(symbol, signal bar) — i.e. the quote the live engine actually had — plus the adverse
slippage assumption. Management: 1m bars (no tick data), intrabar order is unknown so two
orderings are run: adverse_first (stop checked before any favourable move) and
favorable_first. Neither is a bound on live 5-second management; both are approximations.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import analyze as A


class Sim:
    def __init__(self, bars, p, scans, s_list, sig_quote, filters, init_state=None):
        self.bars, self.p, self.scans, self.s_list = bars, p, scans, s_list
        self.sig_quote, self.filters = sig_quote, filters
        self.realized = 0.0
        self.fees = 0.0
        self.positions: dict[str, dict] = {}
        if init_state:
            self.realized = init_state["realized"]
            for P in init_state["positions"]:
                self.positions[P["symbol"]] = {**P, "exits": [], "net": 0.0}
        self.cooldown: dict[str, int] = {}
        self.rev = defaultdict(list)
        self.locked = False
        self.lock_at = None
        self.trades: list[dict] = []
        self.c = Counter()
        self.curve: list[float] = []
        self.n = 0

    # -------------------------------------------------- fills
    def close(self, sym, px, reason, t_ms, qty=None):
        P = self.positions[sym]
        q = P["qty_rem"] if qty is None else min(qty, P["qty_rem"])
        f = A.fee(px * q)
        g = A.pnl(P["side"], P["entry"], px, q)
        self.realized += g - f
        self.fees += f
        P["qty_rem"] -= q
        P["net"] += g - f
        P["exits"].append({"t": A.iso(t_ms), "px": px, "qty": q, "reason": reason, "net": g - f})
        if P["qty_rem"] <= 1e-12:
            self.trades.append(P)
            del self.positions[sym]
            self.cooldown[sym] = t_ms + A.COOLDOWN_MIN * 60_000
            self.c["EXIT_" + reason] += 1

    def try_open(self, sym, side, sg, entry, t_ms, S, bypass_cd=False, linked=None):
        if self.locked:
            self.c["DAILY_LOSS_LOCK"] += 1
            return False
        if sym in self.positions:
            return False
        if len(self.positions) >= A.MAX_POS:
            self.c["MAX_POSITIONS"] += 1
            return False
        used = A.MARGIN * len(self.positions)
        if A.INITIAL + self.realized - used < A.MARGIN:
            self.c["INSUFFICIENT_MARGIN"] += 1
            return False
        if not bypass_cd and self.cooldown.get(sym, 0) > t_ms:
            self.c["COOLDOWN"] += 1
            return False
        atr = sg["atr"]
        stop = entry - self.p["stop_mult"] * atr if side == "LONG" else entry + self.p["stop_mult"] * atr
        step = float(self.filters.get(sym, {}).get("stepSize") or 0.001)
        qty = A.round_step(A.NOTIONAL / entry, step)
        if qty <= 0:
            self.c["LOT_FILTER"] += 1
            return False
        risk = A.risk_est(side, entry, stop, qty)
        if risk > A.MAX_LOSS:
            self.c["RISK_BUDGET_EXCEEDED"] += 1
            return False
        f = A.fee(entry * qty)
        self.realized -= f
        self.fees += f
        self.n += 1
        self.positions[sym] = {
            "tid": f"sim{self.n}:{sym}", "symbol": sym, "side": side, "entry": entry, "qty0": qty, "qty_rem": qty,
            "eff": stop, "init_stop": stop, "stop_kind": "INITIAL_STOP", "extreme": entry, "ptp_done": False,
            "entry_t": A.iso(t_ms), "S": S, "first_manage_S": S + 60_000, "net": -f, "exits": [], "risk": risk,
            "atr": atr, "vol_x": sg["vol_x"], "linked": linked,
        }
        self.c["OPEN"] += 1
        return True

    # -------------------------------------------------- management on one closed bar
    def manage(self, sym, S):
        P = self.positions[sym]
        b = self.bars.get(sym)
        if b is None or S not in b.idx or S < P["first_manage_S"]:
            return
        j = b.idx[S]
        side = P["side"]
        slip_l, slip_s = 1 - A.SLIP, 1 + A.SLIP
        fav = b.h[j] * slip_l if side == "LONG" else b.l[j] * slip_s
        adv = b.l[j] * slip_l if side == "LONG" else b.h[j] * slip_s
        opn = b.o[j] * slip_l if side == "LONG" else b.o[j] * slip_s

        def stop_check():
            eff = P["eff"]
            if (side == "LONG" and adv <= eff) or (side == "SHORT" and adv >= eff):
                px = min(opn, eff) if side == "LONG" else max(opn, eff)
                self.close(sym, px, P["stop_kind"], S + 60_000)
                return True
            return False

        def favourable():
            if not P["ptp_done"]:
                q = P["qty_rem"]
                net_f = A.pnl(side, P["entry"], fav, q) - A.fee(fav * q)
                if net_f >= self.p["ptp"]:
                    # price where net unrealized (gross - exit fee) == threshold
                    if side == "LONG":
                        px = (self.p["ptp"] / q + P["entry"]) / (1 - A.FEE)
                    else:
                        px = (P["entry"] - self.p["ptp"] / q) / (1 + A.FEE)
                    qtp = min(A.round_step(P["qty0"] * A.PTP_FRAC, 0.001), q)
                    self.close(sym, px, "PARTIAL_TP", S + 30_000, qty=qtp)
                    if sym not in self.positions:
                        return
                    P["ptp_done"] = True
                    rem = P["qty_rem"]
                    cost = A.fee(P["entry"] * P["qty0"]) * rem / P["qty0"] + A.fee(P["entry"] * rem)
                    be = P["entry"] + cost / rem if side == "LONG" else P["entry"] - cost / rem
                    if (side == "LONG" and be > P["eff"]) or (side == "SHORT" and be < P["eff"]):
                        P["eff"], P["stop_kind"] = be, "BREAKEVEN_STOP"
            if not self.p.get("extreme_frozen"):
                P["extreme"] = max(P["extreme"], fav) if side == "LONG" else min(P["extreme"], fav)
            if self.p["trail"] == "immediate" or P["ptp_done"]:
                lo = max(0, j + 1 - (A.ATR_P + 5))
                a = A.wilder_atr(b.h[lo : j + 1], b.l[lo : j + 1], b.c[lo : j + 1])
                if a:
                    tr = P["extreme"] - A.TRAIL_MULT * a if side == "LONG" else P["extreme"] + A.TRAIL_MULT * a
                    if (side == "LONG" and tr > P["eff"]) or (side == "SHORT" and tr < P["eff"]):
                        P["eff"], P["stop_kind"] = tr, "TRAILING_STOP"

        if self.p["order"] == "adverse_first":
            if not stop_check():
                favourable()
        else:
            favourable()
            if sym in self.positions:
                stop_check()

    # -------------------------------------------------- main loop
    def run(self):
        for S in self.s_list:
            t_scan = S + 62_000
            for sym in list(self.positions):
                self.manage(sym, S)
            floating = 0.0
            for sym, P in self.positions.items():
                b = self.bars.get(sym)
                if b is not None and S in b.idx:
                    c = b.c[b.idx[S]]
                    m = c * (1 - A.SLIP) if P["side"] == "LONG" else c * (1 + A.SLIP)
                    floating += A.pnl(P["side"], P["entry"], m, P["qty_rem"]) - A.fee(m * P["qty_rem"])
            eqv = A.INITIAL + self.realized + floating
            self.curve.append(eqv)
            if self.p["lock"] is not None and not self.locked and eqv <= A.INITIAL - self.p["lock"]:
                self.locked, self.lock_at = True, A.iso(t_scan)
            cands = []
            for sym in self.scans[S]:
                b = self.bars.get(sym)
                if b is None:
                    self.c["no_bars"] += 1
                    continue
                sg = A.signal_on(b, S)
                if sg is None:
                    self.c["insufficient_bars"] += 1
                    continue
                if not (sg["vol_x"] >= self.p["vol_x"]):
                    self.c["VOL_X"] += 1
                    continue
                if sg["side"] is None:
                    self.c["NO_BREAKOUT"] += 1
                    continue
                q = self.sig_quote.get((sym, S))
                if not q:
                    self.c["QUOTE_MISSING"] += 1
                    continue
                code, raw, chase = A.entry_checks(sg, q["bid"], q["ask"], q["spread"])
                if code in (None, "CHASE"):
                    code = "CHASE" if chase > self.p["chase"] else None
                if code:
                    self.c[code] += 1
                    continue
                self.c["SIGNAL"] += 1
                cands.append((sg["vol_x"], q["spread"], sym, sg, raw, q))
            cands.sort(key=lambda x: (-x[0], x[1], x[2]))
            for _vx, _sp, sym, sg, raw, q in cands:
                side = sg["side"]
                if sym in self.positions:
                    P = self.positions[sym]
                    if P["side"] == side:
                        continue
                    old = P["tid"]
                    self.close(sym, A.mark(P["side"], q["bid"], q["ask"]), "REVERSE_SIGNAL", t_scan)
                    self.rev[sym] = [x for x in self.rev[sym] if t_scan - x <= A.REV_WIN_MIN * 60_000]
                    if len(self.rev[sym]) >= A.MAX_REV:
                        self.c["REVERSE_LIMIT"] += 1
                        continue
                    if self.try_open(sym, side, sg, raw, t_scan, S, bypass_cd=True, linked=old):
                        self.rev[sym].append(t_scan)
                    continue
                self.try_open(sym, side, sg, raw, t_scan, S)
                if len(self.positions) >= A.MAX_POS:
                    break
        # mark remaining at last available close
        floating = 0.0
        for sym, P in self.positions.items():
            b = self.bars[sym]
            c = b.c[-1]
            m = c * (1 - A.SLIP) if P["side"] == "LONG" else c * (1 + A.SLIP)
            floating += A.pnl(P["side"], P["entry"], m, P["qty_rem"]) - A.fee(m * P["qty_rem"])
        peak, mdd = -1e18, 0.0
        for v in self.curve:
            peak = max(peak, v)
            mdd = min(mdd, v - peak)
        closed = self.trades
        nets = [t["net"] for t in closed]
        return {
            "opens": self.c["OPEN"], "closed_trades": len(closed), "open_at_end": len(self.positions),
            "realized_net": self.realized, "fees": self.fees, "floating_end": floating,
            "equity_end": A.INITIAL + self.realized + floating, "mdd_1m_close": mdd,
            "wins": sum(1 for x in nets if x > 0), "losses": sum(1 for x in nets if x <= 0),
            "partial_tp_count": sum(1 for t in closed for e in t["exits"] if e["reason"] == "PARTIAL_TP"),
            "reverse_count": self.c["REVERSE_CLOSE"] if "REVERSE_CLOSE" in self.c else sum(1 for t in closed for e in t["exits"] if e["reason"] == "REVERSE_SIGNAL"),
            "lock_at": self.lock_at, "counters": dict(self.c),
            "trades": [{k: t[k] for k in ("symbol", "side", "entry_t", "entry", "qty0", "init_stop", "risk", "vol_x", "net", "exits")} for t in closed]
            + [{"symbol": s, "side": P["side"], "entry_t": P["entry_t"], "entry": P["entry"], "qty0": P["qty0"], "OPEN_AT_END": True} for s, P in self.positions.items()],
        }


def run_all(arch, truth, scans, s_list, sig_quote, filters, init_state=None):
    base = dict(vol_x=A.VOL_X_MIN, chase=A.MAX_CHASE, stop_mult=A.ATR_MULT, trail="immediate", ptp=A.PTP_NET,
                lock=A.DAILY_LOCK, order="adverse_first", extreme_frozen=True)
    fixed = dict(base, extreme_frozen=False)
    sens = [
        ("vol_x>=2.5", dict(vol_x=2.5)),
        ("vol_x>=4.0", dict(vol_x=4.0)),
        ("chase<=0.5ATR", dict(chase=0.5)),
        ("chase<=1.5ATR", dict(chase=1.5)),
        ("init_stop=2.0ATR(risk cap still 100U)", dict(stop_mult=2.0)),
        ("trail_only_after_partial", dict(trail="after_partial")),
        ("partial_tp_net>=50U", dict(ptp=50.0)),
        ("daily_lock=none(counterfactual)", dict(lock=None)),
    ]
    variants = [
        ("V0_archive_bars_as_coded(live-seen data)", arch, dict(base)),
        ("B0_posthoc_as_coded", truth, dict(base)),
        ("B1_posthoc_extreme_fixed", truth, dict(fixed)),
    ]
    variants += [("B0+" + n, truth, dict(base, **d)) for n, d in sens]
    variants += [("B1+" + n, truth, dict(fixed, **d)) for n, d in sens]
    out = {}
    for name, bars, p in variants:
        r_adv = Sim(bars, dict(p, order="adverse_first"), scans, s_list, sig_quote, filters, init_state).run()
        r_fav = Sim(bars, dict(p, order="favorable_first"), scans, s_list, sig_quote, filters, init_state).run()
        out[name] = {"params": p, "adverse_first": r_adv, "favorable_first": r_fav}
    return out
