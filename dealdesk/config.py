"""Pricing policy, product catalog and market assumptions.

Every number the engine uses lives here so it can be reviewed in one place.
Nothing in this file is fit to real company data; the values are chosen to be
plausible for a hardware + subscription networking vendor and are documented
in docs/DATA.md.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Product catalog
# --------------------------------------------------------------------------
# Hardware is sold once. Each hardware unit requires a per-unit annual license.
# unit_list / unit_cogs are USD price-book values.
SKUS = {
    # sku:          (product_line, description,               hw_list, hw_cogs, lic_list_per_year)
    "EDGE-S":      ("Security", "Branch security appliance",      1_200,     540,   300),
    "EDGE-L":      ("Security", "Campus security appliance",      8_500,   3_570,  1_900),
    "SW-24":       ("Switching", "24-port access switch",         2_400,   1_150,   250),
    "SW-48":       ("Switching", "48-port access switch",         4_600,   2_120,   420),
    "SW-AGG":      ("Switching", "Aggregation switch",           14_000,   6_300,  1_100),
    "AP-IN":       ("Wireless", "Indoor access point",              900,     360,   150),
    "AP-OUT":      ("Wireless", "Outdoor access point",           1_800,     760,   220),
    "CAM-D":       ("Cameras", "Dome camera",                       700,     320,   120),
    "CAM-X":       ("Cameras", "Multi-imager camera",             2_300,   1_010,   200),
}
PRODUCT_LINES = ["Security", "Switching", "Wireless", "Cameras"]

# License delivery cost (cloud hosting, support, updates) as a share of
# license list revenue per year. License gross margin is high but not 100%.
LICENSE_COGS_PCT_OF_LIST = 0.12

# Standard (policy) multi-year license discount, off license list.
# This is published pricing, not a negotiated concession.
TERM_DISCOUNT = {1: 0.00, 3: 0.10, 5: 0.17, 10: 0.25}
TERMS = [1, 3, 5, 10]

# Cost of capital used to discount license cash flows to present value.
DISCOUNT_RATE = 0.09

# --------------------------------------------------------------------------
# Market / geo
# --------------------------------------------------------------------------
# price_book_fx: local currency per USD baked into the local price book.
# freight_pct / duty_pct: landed cost borne by the vendor on hardware (DDP terms),
# as a share of hardware invoice value.
GEOS = {
    #  geo:        region,  currency, price_book_fx, freight_pct, duty_pct
    "US":        ("NA",    "USD",   1.00,   0.020, 0.000),
    "Canada":    ("NA",    "CAD",   1.35,   0.028, 0.000),
    "UK":        ("EMEA",  "GBP",   0.79,   0.032, 0.020),
    "Germany":   ("EMEA",  "EUR",   0.92,   0.030, 0.017),
    "France":    ("EMEA",  "EUR",   0.92,   0.031, 0.017),
    "Japan":     ("APAC",  "JPY",   148.0,  0.038, 0.000),
    "Australia": ("APAC",  "AUD",   1.52,   0.042, 0.050),
    "India":     ("APAC",  "INR",   83.0,   0.040, 0.110),
    "Brazil":    ("LATAM", "BRL",   5.00,   0.055, 0.160),
}
# Annualised FX volatility used to simulate realised rates vs. the price book.
FX_VOL = {"USD": 0.0, "CAD": 0.06, "GBP": 0.08, "EUR": 0.07, "JPY": 0.10,
          "AUD": 0.09, "INR": 0.05, "BRL": 0.15}

# --------------------------------------------------------------------------
# Channel
# --------------------------------------------------------------------------
# Partner margin stack for reseller deals, as a share of the customer invoice:
# distributor margin + reseller margin. Deal registration adds a back-end
# rebate on top.
PARTNER_TIERS = {
    #  tier:        distributor, reseller, deal_reg_rebate, volume_rebate
    "Gold":       (0.03,        0.12,      0.03,            0.02),
    "Silver":     (0.04,        0.10,      0.02,            0.01),
    "Registered": (0.05,        0.08,      0.00,            0.00),
}
DIRECT_REBATE = 0.005  # small loyalty / MDF accrual on direct deals

# Payment terms: carrying cost = days / 365 * DISCOUNT_RATE * invoice value.
PAYMENT_TERMS_DAYS = [30, 60, 90]

# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------
SEGMENTS = ["Enterprise", "Mid-Market", "SMB", "SLED", "Education"]

# Minimum PV gross margin (on pocket revenue) a deal must clear, by channel.
# Below the floor the deal fails regardless of tier and needs a CFO exception.
# Reseller floors sit lower because the partner absorbs pre-sales, delivery and
# first-line support that a direct deal carries in opex. Floors are set near
# the 15th percentile of each segment x channel in the closed book, so roughly
# one deal in seven historically needed an exception.
MARGIN_FLOOR = {
    #               direct, reseller
    "Enterprise": (0.42, 0.30),
    "Mid-Market": (0.45, 0.33),
    "SMB":        (0.48, 0.36),
    "SLED":       (0.42, 0.28),
    "Education":  (0.40, 0.26),
}


def margin_floor(segment: str, channel: str) -> float:
    direct, reseller = MARGIN_FLOOR[segment]
    return reseller if channel == "Reseller" else direct


# --------------------------------------------------------------------------
# Approval routing matrix
# --------------------------------------------------------------------------
# Deal size bands on total contract value (USD list, full term).
SIZE_BANDS = [
    ("<$50K", 0, 50_000),
    ("$50K-250K", 50_000, 250_000),
    ("$250K-1M", 250_000, 1_000_000),
    (">$1M", 1_000_000, float("inf")),
]

# Max negotiated discount each tier may approve, by segment x size band.
# Read as: in Enterprise $250K-1M, a rep can approve up to 15%, a manager up
# to 25%, a VP up to 35%; anything above goes to the CFO.
TIERS = ["Rep", "Manager", "VP", "CFO"]
APPROVAL_MATRIX = {
    "Enterprise": {"<$50K": (0.10, 0.20, 0.30), "$50K-250K": (0.12, 0.22, 0.32),
                   "$250K-1M": (0.15, 0.25, 0.35), ">$1M": (0.15, 0.28, 0.38)},
    "Mid-Market": {"<$50K": (0.08, 0.18, 0.28), "$50K-250K": (0.10, 0.20, 0.30),
                   "$250K-1M": (0.12, 0.22, 0.32), ">$1M": (0.12, 0.24, 0.34)},
    "SMB":        {"<$50K": (0.05, 0.15, 0.25), "$50K-250K": (0.08, 0.18, 0.26),
                   "$250K-1M": (0.10, 0.20, 0.28), ">$1M": (0.10, 0.20, 0.28)},
    "SLED":       {"<$50K": (0.10, 0.20, 0.30), "$50K-250K": (0.12, 0.24, 0.34),
                   "$250K-1M": (0.15, 0.27, 0.37), ">$1M": (0.18, 0.30, 0.40)},
    "Education":  {"<$50K": (0.12, 0.22, 0.32), "$50K-250K": (0.15, 0.25, 0.35),
                   "$250K-1M": (0.18, 0.28, 0.38), ">$1M": (0.20, 0.30, 0.40)},
}

# --------------------------------------------------------------------------
# Peer benchmarking and counter-offers
# --------------------------------------------------------------------------
PEER_K = 30                 # comparable deals per request
PEER_MIN = 12               # below this, widen the match
PEER_BAND = (0.25, 0.75)    # inside-band range of peer discounts
PEER_OUTLIER_Q = 0.90       # above this percentile = outlier

# Counter-offer levers.
VOLUME_COMMIT_UPLIFT = 0.25     # customer commits to +25% units
VOLUME_TIER_EXTRA_DISC = 0.03   # ...and earns a 3 pt published volume tier
ATTACH_LICENSE_RATIO = 0.5      # attach line sized at 50% of the main unit count
