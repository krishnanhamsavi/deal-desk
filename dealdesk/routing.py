"""Approval routing: segment x deal size thresholds plus a margin floor."""
from __future__ import annotations

from dataclasses import dataclass

from . import config as C
from .economics import Deal, summarize


@dataclass
class Route:
    tier: str               # Rep | Manager | VP | CFO
    floor_pass: bool
    margin_floor: float
    margin_pv_pct: float
    negotiated_discount: float
    size_band: str
    thresholds: dict        # tier -> max discount that tier can approve
    reasons: list

    @property
    def status(self) -> str:
        return "PASS" if self.floor_pass else "FAIL: below margin floor"


def route(d: Deal, s: dict | None = None) -> Route:
    s = s or summarize(d)
    band = s["size_band"]
    rep_max, mgr_max, vp_max = C.APPROVAL_MATRIX[d.segment][band]
    disc = s["negotiated_discount"]
    floor = C.margin_floor(d.segment, d.channel)
    reasons = []

    if disc <= rep_max + 1e-9:
        tier = "Rep"
    elif disc <= mgr_max + 1e-9:
        tier = "Manager"
    elif disc <= vp_max + 1e-9:
        tier = "VP"
    else:
        tier = "CFO"
    reasons.append(f"{disc:.1%} negotiated discount in {d.segment} {band} routes to {tier}")

    floor_pass = s["margin_pv_pct"] >= floor
    if not floor_pass:
        reasons.append(f"PV margin {s['margin_pv_pct']:.1%} is below the {floor:.0%} "
                       f"{d.segment} {d.channel.lower()} floor; CFO exception required")
        tier = "CFO"

    return Route(tier=tier, floor_pass=floor_pass, margin_floor=floor,
                 margin_pv_pct=s["margin_pv_pct"], negotiated_discount=disc,
                 size_band=band,
                 thresholds={"Rep": rep_max, "Manager": mgr_max, "VP": vp_max},
                 reasons=reasons)


def tier_rank(tier: str) -> int:
    return C.TIERS.index(tier)
