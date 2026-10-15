"""One discount request in, a complete deal desk read-out out.

analyze() is the single entry point the console and the memo writer use. It
returns plain numbers; nothing downstream recomputes anything.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from . import config as C
from .db import band_status, find_peers
from .economics import Deal, margin, summarize, term_trade
from .routing import Route, route


@dataclass
class PeerBand:
    n: int
    match: str
    p25: float
    median: float
    p75: float
    p90: float
    status: str
    margin_at_stake: float      # PV margin $ given up vs. peer median
    peers: pd.DataFrame


@dataclass
class CounterPath:
    name: str
    lever: str
    discount: float             # blended negotiated discount granted
    term_years: int
    tcv_list: float
    pocket_price: float
    revenue_pv: float
    margin_pv: float
    margin_pv_pct: float
    tier: str
    floor_pass: bool
    customer_ask: str

    def as_row(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Analysis:
    deal: Deal
    summary: dict
    route: Route
    band: PeerBand
    counters: list
    recommended: CounterPath
    term_trade: list


# --------------------------------------------------------------------------
def peer_band(con, d: Deal, s: dict) -> PeerBand:
    peers, match = find_peers(con, d, s)
    x = peers.negotiated_discount.to_numpy()
    p25, med, p75 = np.quantile(x, [C.PEER_BAND[0], 0.5, C.PEER_BAND[1]])
    p90 = np.quantile(x, C.PEER_OUTLIER_Q)
    req = s["negotiated_discount"]
    at_stake = 0.0
    if req > med:
        at_stake = margin(d.with_discount(med)).margin_pv - s["margin_pv"]
    return PeerBand(n=len(peers), match=match, p25=float(p25), median=float(med), p75=float(p75),
                    p90=float(p90), status=band_status(req, p25, p75, p90),
                    margin_at_stake=float(at_stake), peers=peers)


def _path(name, lever, d: Deal, ask: str) -> CounterPath:
    s = summarize(d)
    r = route(d, s)
    return CounterPath(name=name, lever=lever, discount=s["negotiated_discount"], term_years=d.term_years,
                       tcv_list=s["list_price"], pocket_price=s["pocket_price"], revenue_pv=s["revenue_pv"],
                       margin_pv=s["margin_pv"], margin_pv_pct=s["margin_pv_pct"], tier=r.tier,
                       floor_pass=r.floor_pass, customer_ask=ask)


def _max_discount(d: Deal, cap: float, margin_target: float, floor: float) -> float | None:
    """Largest blended discount <= cap where PV margin $ >= target and PV
    margin % >= floor. Margin is monotone decreasing in discount, so bisect."""
    def ok(x):
        m = margin(d.with_discount(x))
        return m.margin_pv >= margin_target - 1e-6 and m.margin_pv_pct >= floor - 1e-9
    if ok(cap):
        return cap
    if not ok(0.0):
        return None
    lo, hi = 0.0, cap
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid)
    return np.floor(lo * 1000) / 1000   # round down to 0.1 pt


def _attach_line(d: Deal) -> tuple[str, int] | None:
    present = {C.SKUS[s][0] for s, _ in d.lines}
    # Natural cross-sells: switching -> wireless, wireless -> security, etc.
    prefs = ["Wireless", "Security", "Cameras", "Switching"]
    sku_for = {"Wireless": "AP-IN", "Security": "EDGE-S", "Cameras": "CAM-D", "Switching": "SW-24"}
    for line in prefs:
        if line not in present:
            units = sum(q for _, q in d.lines)
            return sku_for[line], max(1, int(units * C.ATTACH_LICENSE_RATIO))
    return None


def counter_offers(d: Deal, s: dict, band: PeerBand) -> tuple[list, CounterPath]:
    req = s["negotiated_discount"]
    floor = C.margin_floor(d.segment, d.channel)
    paths = [_path("As requested", "none", d, "No change")]

    # Path 1: price-only counter. Hold at the peer median, or lower if the
    # median itself breaches the margin floor on this deal's structure.
    floor_max = _max_discount(d, cap=req, margin_target=-np.inf, floor=floor)
    floor_max = 0.0 if floor_max is None else floor_max
    if floor_max < min(req, band.median) - 1e-9:
        hold, hold_name, hold_ask = floor_max, "Hold at margin floor", "Accept the floor-compliant discount"
    elif req <= band.median:
        hold, hold_name, hold_ask = req, "Hold at request", "No change"
    else:
        hold, hold_name, hold_ask = band.median, "Hold at peer median", "Accept the benchmark discount"
    hold_path = _path(hold_name, "price", d.with_discount(hold), hold_ask)
    paths.append(hold_path)
    # Every trade must earn at least what holding at the median would have.
    target = hold_path.margin_pv

    trades = []
    nxt = {1: 5, 3: 5, 5: 10}.get(d.term_years)
    if nxt:
        trades.append((f"{nxt}-year term", "term", replace(d, term_years=nxt),
                       f"Extend license term from {d.term_years} to {nxt} years"))

    vol = tuple((sku, int(np.ceil(q * (1 + C.VOLUME_COMMIT_UPLIFT)))) for sku, q in d.lines)
    trades.append(("Volume commitment", "volume", replace(d, lines=vol),
                   f"Commit to {C.VOLUME_COMMIT_UPLIFT:.0%} more units in this order"))

    att = _attach_line(d)
    if att:
        sku, n = att
        trades.append((f"Attach {C.SKUS[sku][0]}", "attach", replace(d, lines=d.lines + ((sku, n),)),
                       f"Add {n} x {sku} ({C.SKUS[sku][1].lower()})"))

    for name, lever, alt, ask in trades:
        # A counter should be signable below the CFO: cap it at VP authority.
        vp_max = C.APPROVAL_MATRIX[alt.segment][summarize(alt)["size_band"]][2]
        x = _max_discount(alt, cap=min(req, vp_max), margin_target=target, floor=floor)
        if x is None or x < hold - 1e-9:
            continue    # the trade does not beat simply holding price
        p = _path(name, lever, alt.with_discount(x), ask)
        p.name = f"{x:.1%} with {name.lower()}"
        paths.append(p)

    # Recommend the trade that gives the customer the most discount while
    # passing the floor and earning >= the hold path; tie-break on margin $.
    # A request that is inside the peer band and clears the floor is simply
    # approved; the trades are still shown as upside.
    if paths[0].floor_pass and band.status in ("In band", "Below band"):
        return paths, paths[0]
    feasible = [p for p in paths[1:] if p.floor_pass and p.margin_pv >= target - 1e-6]
    rec = max(feasible, key=lambda p: (round(p.discount, 4), p.margin_pv)) if feasible else hold_path
    return paths, rec


def analyze(con, d: Deal) -> Analysis:
    s = summarize(d)
    r = route(d, s)
    band = peer_band(con, d, s)
    paths, rec = counter_offers(d, s, band)
    return Analysis(deal=d, summary=s, route=r, band=band, counters=paths, recommended=rec,
                    term_trade=term_trade(d))
