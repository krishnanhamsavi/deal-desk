"""Generate the synthetic deal book.

    python -m dealdesk.generate            # writes data/dealbook.duckdb
    python -m dealdesk.generate --seed 7   # different draw, same distributions

The distributions are documented inline and in docs/DATA.md. The point is not
realism to the decimal; it is that every shape in the data comes from a stated
assumption a reviewer can argue with, not from a hidden fudge.

Book: 3,920 closed-won deals (2023Q1 to 2026Q3) that form the precedent set,
plus 80 pending discount requests in 2026Q4 that form the desk queue.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .economics import Deal, summarize

N_CLOSED = 3_920
N_PENDING = 80
QUARTERS = [f"{y}Q{q}" for y in (2023, 2024, 2025, 2026) for q in (1, 2, 3, 4)][:15]
PENDING_Q = "2026Q4"

# Segment mix of deal count.
SEGMENT_W = {"Enterprise": 0.18, "Mid-Market": 0.27, "SMB": 0.30, "SLED": 0.13, "Education": 0.12}

# Hardware list value per deal: lognormal(median, sigma). Enterprise deals are
# ~15x SMB at the median, with a long right tail.
HW_MEDIAN = {"Enterprise": 220_000, "Mid-Market": 70_000, "SMB": 15_000, "SLED": 110_000, "Education": 60_000}
HW_SIGMA = 0.85

# P(reseller) by segment. SMB and public sector buy through the channel.
RESELLER_P = {"Enterprise": 0.45, "Mid-Market": 0.70, "SMB": 0.85, "SLED": 0.75, "Education": 0.80}
PARTNER_TIER_W = {"Gold": 0.35, "Silver": 0.40, "Registered": 0.25}

# License term mix by segment (1 / 3 / 5 / 10 years).
TERM_W = {
    "Enterprise": [0.10, 0.45, 0.38, 0.07],
    "Mid-Market": [0.25, 0.50, 0.22, 0.03],
    "SMB":        [0.50, 0.38, 0.12, 0.00],
    "SLED":       [0.05, 0.35, 0.40, 0.20],
    "Education":  [0.08, 0.40, 0.35, 0.17],
}

# Primary product line by segment.
LINE_W = {
    "Enterprise": [0.35, 0.40, 0.18, 0.07],   # Security, Switching, Wireless, Cameras
    "Mid-Market": [0.30, 0.30, 0.28, 0.12],
    "SMB":        [0.35, 0.15, 0.35, 0.15],
    "SLED":       [0.20, 0.30, 0.25, 0.25],
    "Education":  [0.10, 0.30, 0.45, 0.15],
}
EXTRA_LINES_W = {"Enterprise": [0.30, 0.45, 0.25], "Mid-Market": [0.50, 0.38, 0.12],
                 "SMB": [0.72, 0.25, 0.03], "SLED": [0.40, 0.42, 0.18], "Education": [0.45, 0.40, 0.15]}

# Country mix within each region; region mix of reps.
GEO_IN_REGION = {
    "NA": {"US": 0.85, "Canada": 0.15},
    "EMEA": {"UK": 0.40, "Germany": 0.35, "France": 0.25},
    "APAC": {"Japan": 0.40, "Australia": 0.35, "India": 0.25},
    "LATAM": {"Brazil": 1.0},
}
REGION_W = {"NA": 0.55, "EMEA": 0.25, "APAC": 0.14, "LATAM": 0.06}
PAY_W = {"Enterprise": [0.40, 0.40, 0.20], "Mid-Market": [0.65, 0.30, 0.05], "SMB": [0.85, 0.15, 0.0],
         "SLED": [0.30, 0.45, 0.25], "Education": [0.35, 0.45, 0.20]}

# Negotiated discount model (blended, off standard price):
#   base[segment] + 0.025*ln(size/median) + rep_effect + Q4 push
#   + reseller competitive pressure + term effect + noise
DISC_BASE = {"Enterprise": 0.19, "Mid-Market": 0.15, "SMB": 0.10, "SLED": 0.21, "Education": 0.23}
Q4_PUSH = 0.025
RESELLER_PUSH = 0.035          # channel conflict / competitive displacement
RESELLER_NOISE = 0.065         # fatter tail on channel deals
DIRECT_NOISE = 0.045
TERM_PUSH_PER_YEAR = 0.006     # reps trade hardware margin for term
N_REPS = 64


def _pick(rng, weights: dict):
    k = list(weights)
    return k[rng.choice(len(k), p=np.array(list(weights.values())) / sum(weights.values()))]


def _fx_paths(rng) -> dict:
    """Quarter-end FX per currency: geometric random walk from the price-book
    rate with quarterly vol = annual vol / 2."""
    paths = {}
    for geo, (_, ccy, book, _, _) in C.GEOS.items():
        if ccy in paths:
            continue
        vol = C.FX_VOL[ccy] / 2
        shocks = rng.normal(0, vol, len(QUARTERS) + 1)
        shocks[0] = 0
        levels = book * np.exp(np.cumsum(shocks))
        paths[ccy] = dict(zip(QUARTERS + [PENDING_Q], levels))
    return paths


def _reps(rng) -> pd.DataFrame:
    rows = []
    for i in range(N_REPS):
        region = _pick(rng, REGION_W)
        seg = _pick(rng, SEGMENT_W)
        # Most reps sit near policy; a handful habitually over-discount.
        eff = rng.normal(0, 0.025) + (0.06 if rng.random() < 0.10 else 0.0)
        rows.append({"rep": f"R{i + 1:03d}", "region": region, "home_segment": seg, "rep_effect": eff})
    return pd.DataFrame(rows)


def _lines(rng, segment: str, hw_target: float) -> list:
    lw = dict(zip(C.PRODUCT_LINES, LINE_W[segment]))
    primary = _pick(rng, lw)
    n_extra = rng.choice(3, p=EXTRA_LINES_W[segment])
    others = [p for p in C.PRODUCT_LINES if p != primary]
    extra = list(rng.choice(others, size=n_extra, replace=False)) if n_extra else []
    shares = [1.0] if not extra else list(rng.dirichlet([4] + [1.5] * len(extra)))
    lines = []
    for line, share in zip([primary] + extra, shares):
        skus = [s for s, v in C.SKUS.items() if v[0] == line]
        n_sku = 1 if rng.random() < 0.55 else min(2, len(skus))
        chosen = rng.choice(skus, size=n_sku, replace=False)
        split = [1.0] if n_sku == 1 else list(rng.dirichlet([2] * n_sku))
        for sku, sp in zip(chosen, split):
            qty = max(1, int(round(hw_target * share * sp / C.SKUS[sku][2])))
            lines.append((str(sku), qty))
    return lines


def _split_discount(rng, d_partial: Deal, blended: float, term: int) -> tuple:
    """Turn a blended discount into hardware and license discounts. On longer
    terms reps load the concession onto hardware, which is the behaviour the
    blended margin view exists to expose."""
    from .economics import hw_list, lic_list, term_discount_amt
    H = hw_list(d_partial)
    S = lic_list(d_partial) - term_discount_amt(d_partial)
    tilt = {1: -0.01, 3: 0.02, 5: 0.05, 10: 0.07}[term] + rng.normal(0, 0.015)
    hw = float(np.clip(blended + tilt, 0, 0.70))
    lic = (blended * (H + S) - H * hw) / S if S > 0 else 0.0
    lic = float(np.clip(lic, 0, 0.60))
    return hw, lic


def generate(seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    fx = _fx_paths(rng)
    reps = _reps(rng)

    q_w = np.array([{1: 0.85, 2: 0.95, 3: 1.0, 4: 1.35}[int(q[-1])] * (1.04 ** (int(q[:4]) - 2023))
                    for q in QUARTERS])
    q_w /= q_w.sum()

    deals, lines_rows = [], []
    for i in range(N_CLOSED + N_PENDING):
        pending = i >= N_CLOSED
        rep = reps.iloc[rng.integers(len(reps))]
        # 70% of a rep's deals are in their home segment
        segment = rep.home_segment if rng.random() < 0.7 else _pick(rng, SEGMENT_W)
        geo = _pick(rng, GEO_IN_REGION[rep.region])
        quarter = PENDING_Q if pending else QUARTERS[rng.choice(len(QUARTERS), p=q_w)]
        channel = "Reseller" if rng.random() < RESELLER_P[segment] else "Direct"
        tier = _pick(rng, PARTNER_TIER_W) if channel == "Reseller" else None
        term = int(rng.choice(C.TERMS, p=TERM_W[segment]))
        pay = int(rng.choice(C.PAYMENT_TERMS_DAYS, p=PAY_W[segment]))
        hw_target = float(rng.lognormal(np.log(HW_MEDIAN[segment]), HW_SIGMA))
        lines = _lines(rng, segment, hw_target)

        blended = (DISC_BASE[segment]
                   + 0.025 * np.log(hw_target / HW_MEDIAN[segment])
                   + rep.rep_effect
                   + (Q4_PUSH if quarter.endswith("Q4") else 0.0)
                   + (RESELLER_PUSH if channel == "Reseller" else 0.0)
                   + TERM_PUSH_PER_YEAR * (term - 1)
                   + rng.normal(0, RESELLER_NOISE if channel == "Reseller" else DIRECT_NOISE))
        if pending:
            blended += 0.03  # requests open high; the desk negotiates down
        blended = float(np.clip(blended, 0.0, 0.55))

        deal_id = f"{'REQ' if pending else 'D'}-{i + 1:05d}"
        base = Deal(segment=segment, geo=geo, channel=channel, partner_tier=tier,
                    term_years=term, lines=tuple(lines), hw_discount=0, lic_discount=0)
        hw_d, lic_d = _split_discount(rng, base, blended, term)
        d = Deal(segment=segment, geo=geo, channel=channel, partner_tier=tier, term_years=term,
                 lines=tuple(lines), hw_discount=hw_d, lic_discount=lic_d, payment_days=pay,
                 fx_realized=float(fx[C.GEOS[geo][1]][quarter]), deal_id=deal_id, rep=rep.rep,
                 close_quarter=quarter, account=f"ACCT-{rng.integers(10_000, 99_999)}")

        deals.append({
            "deal_id": deal_id, "status": "Pending" if pending else "Closed Won",
            "account": d.account, "rep": d.rep, "segment": segment, "geo": geo,
            "region": C.GEOS[geo][0], "currency": C.GEOS[geo][1], "channel": channel,
            "partner_tier": tier, "term_years": term, "payment_days": pay,
            "hw_discount": hw_d, "lic_discount": lic_d, "fx_realized": d.fx_realized,
            "close_quarter": quarter, **summarize(d),
        })
        for sku, qty in lines:
            lines_rows.append({"deal_id": deal_id, "sku": sku, "product_line": C.SKUS[sku][0], "qty": qty})

    return pd.DataFrame(deals), pd.DataFrame(lines_rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "dealbook.duckdb"))
    a = ap.parse_args()
    from .db import write_book
    deals, lines = generate(a.seed)
    write_book(deals, lines, a.out)
    print(f"wrote {len(deals):,} deals / {len(lines):,} lines to {a.out}")


if __name__ == "__main__":
    main()
