"""Deal economics: pocket price waterfall and blended PV margin.

All functions are pure and deterministic. A Deal goes in, numbers come out.

Waterfall framing follows Marn & Rosiello ("Managing Price, Gaining Profit",
HBR 1992): list price -> on-invoice concessions -> invoice price ->
off-invoice leakage -> pocket price. Pocket margin is pocket price less COGS.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import config as C


@dataclass(frozen=True)
class Deal:
    segment: str
    geo: str
    channel: str                     # "Direct" | "Reseller"
    term_years: int
    lines: tuple                     # ((sku, qty), ...)
    hw_discount: float               # negotiated discount off hardware list
    lic_discount: float              # negotiated discount off standard (post-term) license price
    partner_tier: str | None = None  # required when channel == "Reseller"
    payment_days: int = 30
    fx_realized: float | None = None  # local per USD at booking; None = price-book rate
    deal_id: str = "NEW"
    rep: str = ""
    close_quarter: str = ""
    account: str = ""

    def with_discount(self, blended: float) -> "Deal":
        """Same deal, with hw and license discounts scaled so the blended
        negotiated discount equals `blended`."""
        cur = negotiated_discount(self)
        if cur <= 0:
            return replace(self, hw_discount=blended, lic_discount=blended)
        f = blended / cur
        return replace(self, hw_discount=min(self.hw_discount * f, 0.95),
                       lic_discount=min(self.lic_discount * f, 0.95))


# --------------------------------------------------------------------------
# list-price building blocks
# --------------------------------------------------------------------------
def hw_list(d: Deal) -> float:
    return sum(C.SKUS[s][2] * q for s, q in d.lines)


def hw_cogs(d: Deal) -> float:
    return sum(C.SKUS[s][3] * q for s, q in d.lines)


def lic_list_annual(d: Deal) -> float:
    return sum(C.SKUS[s][4] * q for s, q in d.lines)


def lic_list(d: Deal) -> float:
    return lic_list_annual(d) * d.term_years


def term_discount_amt(d: Deal) -> float:
    return lic_list(d) * C.TERM_DISCOUNT[d.term_years]


def standard_price(d: Deal) -> float:
    """List less published term discount: the price before any negotiation."""
    return hw_list(d) + lic_list(d) - term_discount_amt(d)


def negotiated_discount(d: Deal) -> float:
    """Blended negotiated discount off standard price. This is the number the
    approval matrix and peer bands are written against."""
    std = standard_price(d)
    if std <= 0:
        return 0.0
    std_lic = lic_list(d) - term_discount_amt(d)
    return (hw_list(d) * d.hw_discount + std_lic * d.lic_discount) / std


def product_mix(d: Deal) -> dict:
    mix = {pl: 0.0 for pl in C.PRODUCT_LINES}
    for s, q in d.lines:
        mix[C.SKUS[s][0]] += C.SKUS[s][2] * q
    tot = sum(mix.values()) or 1.0
    return {k: v / tot for k, v in mix.items()}


def primary_line(d: Deal) -> str:
    m = product_mix(d)
    return max(m, key=m.get)


def size_band(tcv: float) -> str:
    for name, lo, hi in C.SIZE_BANDS:
        if lo <= tcv < hi:
            return name
    return C.SIZE_BANDS[-1][0]


# --------------------------------------------------------------------------
# waterfall
# --------------------------------------------------------------------------
@dataclass
class Waterfall:
    steps: list = field(default_factory=list)   # [(label, amount, kind)]
    hw_pocket: float = 0.0
    lic_pocket: float = 0.0

    @property
    def list_price(self) -> float:
        return self.steps[0][1]

    @property
    def invoice_price(self) -> float:
        return next(a for l, a, k in self.steps if l == "Invoice price")

    @property
    def pocket_price(self) -> float:
        return self.steps[-1][1]

    def as_rows(self) -> list[dict]:
        return [{"step": l, "amount": a, "kind": k} for l, a, k in self.steps]


def waterfall(d: Deal) -> Waterfall:
    """Full-term contract waterfall in USD (nominal, undiscounted)."""
    region, ccy, book_fx, freight, duty = C.GEOS[d.geo]
    L_hw, L_lic = hw_list(d), lic_list(d)
    term_disc = term_discount_amt(d)
    std_lic = L_lic - term_disc
    neg_hw = L_hw * d.hw_discount
    neg_lic = std_lic * d.lic_discount

    inv_hw = L_hw - neg_hw
    inv_lic = std_lic - neg_lic
    invoice = inv_hw + inv_lic

    if d.channel == "Reseller":
        dist, resell, deal_reg, vol = C.PARTNER_TIERS[d.partner_tier or "Registered"]
        partner_pct, rebate_pct = dist + resell, deal_reg + vol
    else:
        partner_pct, rebate_pct = 0.0, C.DIRECT_REBATE
    partner = invoice * partner_pct
    net_to_vendor = invoice - partner
    rebates = invoice * rebate_pct

    # Hardware is invoiced up front, license annually in advance: only the
    # first-year license carries payment-terms cost.
    hw_share = inv_hw / invoice if invoice else 0.0
    first_bill = net_to_vendor * (hw_share + (1 - hw_share) / d.term_years)
    pay_cost = first_bill * d.payment_days / 365 * C.DISCOUNT_RATE

    landed = inv_hw * (1 - partner_pct) * (freight + duty)

    # Price book converts at book_fx; vendor books USD at the realized rate.
    fx_real = d.fx_realized or book_fx
    fx_var = (net_to_vendor - rebates) * (book_fx / fx_real - 1)

    pocket = net_to_vendor - rebates - pay_cost - landed + fx_var

    steps = [
        ("List price", L_hw + L_lic, "total"),
        ("Term discount", -term_disc, "on-invoice"),
        ("Negotiated discount: hardware", -neg_hw, "on-invoice"),
        ("Negotiated discount: license", -neg_lic, "on-invoice"),
        ("Invoice price", invoice, "total"),
        ("Partner margin", -partner, "off-invoice"),
        ("Rebates & deal reg", -rebates, "off-invoice"),
        ("Payment terms", -pay_cost, "off-invoice"),
        ("Freight & duty", -landed, "off-invoice"),
        ("FX variance", fx_var, "off-invoice"),
        ("Pocket price", pocket, "total"),
    ]
    # Split pocket into hardware and license. Landed cost is hardware-only;
    # every other deduction is shared pro rata on invoice value.
    shared = pocket + landed
    hw_pocket = shared * hw_share - landed
    lic_pocket = shared * (1 - hw_share)
    return Waterfall(steps=steps, hw_pocket=hw_pocket, lic_pocket=lic_pocket)


# --------------------------------------------------------------------------
# blended margin (PV)
# --------------------------------------------------------------------------
@dataclass
class Margin:
    hw_revenue: float
    hw_cogs: float
    lic_revenue_nominal: float
    lic_cogs_nominal: float
    lic_revenue_pv: float
    lic_cogs_pv: float

    @property
    def hw_margin(self):
        return self.hw_revenue - self.hw_cogs

    @property
    def hw_margin_pct(self):
        return self.hw_margin / self.hw_revenue if self.hw_revenue else 0.0

    @property
    def lic_margin_pv(self):
        return self.lic_revenue_pv - self.lic_cogs_pv

    @property
    def lic_margin_pct(self):
        return self.lic_margin_pv / self.lic_revenue_pv if self.lic_revenue_pv else 0.0

    @property
    def revenue_pv(self):
        return self.hw_revenue + self.lic_revenue_pv

    @property
    def margin_pv(self):
        return self.hw_margin + self.lic_margin_pv

    @property
    def margin_pv_pct(self):
        return self.margin_pv / self.revenue_pv if self.revenue_pv else 0.0

    @property
    def revenue_nominal(self):
        return self.hw_revenue + self.lic_revenue_nominal

    @property
    def margin_nominal(self):
        return self.hw_margin + self.lic_revenue_nominal - self.lic_cogs_nominal


def annuity_factor(years: int, rate: float = C.DISCOUNT_RATE) -> float:
    """PV of 1 per year paid in advance for `years` years."""
    return sum(1 / (1 + rate) ** t for t in range(years))


def margin(d: Deal, wf: Waterfall | None = None) -> Margin:
    wf = wf or waterfall(d)
    T = d.term_years
    af = annuity_factor(T)
    lic_rev_yr = wf.lic_pocket / T
    lic_cogs_yr = lic_list_annual(d) * C.LICENSE_COGS_PCT_OF_LIST
    return Margin(
        hw_revenue=wf.hw_pocket,
        hw_cogs=hw_cogs(d),
        lic_revenue_nominal=wf.lic_pocket,
        lic_cogs_nominal=lic_cogs_yr * T,
        lic_revenue_pv=lic_rev_yr * af,
        lic_cogs_pv=lic_cogs_yr * af,
    )


def _slug(label: str) -> str:
    return (label.lower().replace("negotiated discount: ", "neg_").replace(" & ", "_")
            .replace(" ", "_"))


def summarize(d: Deal) -> dict:
    """Flat dict of every headline metric for a deal."""
    wf = waterfall(d)
    m = margin(d, wf)
    tcv = hw_list(d) + lic_list(d)
    mix = product_mix(d)
    wf_cols = {f"wf_{_slug(l)}": a for l, a, k in wf.steps if k != "total"}
    return {
        **wf_cols,
        "list_price": wf.list_price,
        "standard_price": standard_price(d),
        "invoice_price": wf.invoice_price,
        "pocket_price": wf.pocket_price,
        "pocket_pct_of_list": wf.pocket_price / wf.list_price if wf.list_price else 0.0,
        "negotiated_discount": negotiated_discount(d),
        "hw_list": hw_list(d),
        "lic_list": lic_list(d),
        "hw_share": hw_list(d) / tcv if tcv else 0.0,
        "primary_line": max(mix, key=mix.get),
        "n_lines": sum(1 for v in mix.values() if v > 0),
        "size_band": size_band(tcv),
        "hw_margin": m.hw_margin,
        "hw_margin_pct": m.hw_margin_pct,
        "lic_margin_pv": m.lic_margin_pv,
        "lic_margin_pct": m.lic_margin_pct,
        "revenue_pv": m.revenue_pv,
        "margin_pv": m.margin_pv,
        "margin_pv_pct": m.margin_pv_pct,
        "revenue_nominal": m.revenue_nominal,
        "margin_nominal": m.margin_nominal,
    }


def term_trade(d: Deal) -> list[dict]:
    """The rep's trade, made explicit: for each available term, the hardware
    discount at which PV margin dollars equal the deal as submitted.

    A rep asking for deep hardware discount 'because it's a 5-year deal' is
    claiming the license tail pays it back. This shows whether it does.
    """
    base = margin(d).margin_pv
    out = []
    for T in C.TERMS:
        alt = replace(d, term_years=T)
        lo, hi = 0.0, 0.95
        # margin is monotone decreasing in hw discount: bisect for break-even
        if margin(replace(alt, hw_discount=lo)).margin_pv < base:
            be = None
        elif margin(replace(alt, hw_discount=hi)).margin_pv >= base:
            be = hi
        else:
            for _ in range(50):
                mid = (lo + hi) / 2
                if margin(replace(alt, hw_discount=mid)).margin_pv >= base:
                    lo = mid
                else:
                    hi = mid
            be = lo
        m_alt = margin(alt)
        out.append({
            "term_years": T,
            "margin_pv_at_requested": m_alt.margin_pv,
            "margin_pv_pct_at_requested": m_alt.margin_pv_pct,
            "breakeven_hw_discount": be,
        })
    return out
