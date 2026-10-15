"""Deal desk approval memo.

Architecture: the engine computes every figure; this module formats those
figures into a FACTS block, and a language model writes prose around them. The model is
told never to compute, and the output is checked: any number in the memo that
does not appear in FACTS is surfaced to the reviewer as unverified.

Without an API key (or if the call fails) a deterministic template memo is
produced from the same FACTS, so the console always works offline.
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import config as C
from .analysis import Analysis

MODEL = "claude-opus-5-5"


def usd(x: float) -> str:
    x = round(x)
    return f"-${abs(x):,}" if x < 0 else f"${x:,}"


def pct(x: float) -> str:
    return f"{x:.1%}"


# --------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------
def build_facts(a: Analysis, n_precedents: int = 5) -> dict:
    d, s, r, b = a.deal, a.summary, a.route, a.band
    precedents = []
    for p in b.peers.head(n_precedents).itertuples():
        precedents.append({
            "deal_id": p.deal_id, "quarter": p.close_quarter, "geo": p.geo, "channel": p.channel,
            "term": f"{p.term_years}-year", "list_value": usd(p.list_price),
            "negotiated_discount": pct(p.negotiated_discount), "pv_margin": pct(p.margin_pv_pct),
        })
    rec = a.recommended
    return {
        "request": {
            "deal_id": d.deal_id, "account": d.account, "rep": d.rep, "segment": d.segment,
            "geo": d.geo, "channel": d.channel + (f" ({d.partner_tier} partner)" if d.partner_tier else ""),
            "term": f"{d.term_years}-year", "close_quarter": d.close_quarter,
            "products": ", ".join(f"{q} x {sku}" for sku, q in d.lines),
            "size_band": s["size_band"],
        },
        "pricing": {
            "list_price": usd(s["list_price"]),
            "requested_negotiated_discount": pct(s["negotiated_discount"]),
            "hardware_discount": pct(d.hw_discount), "license_discount": pct(d.lic_discount),
            "invoice_price": usd(s["invoice_price"]),
            "pocket_price": usd(s["pocket_price"]),
            "pocket_as_pct_of_list": pct(s["pocket_pct_of_list"]),
            "waterfall": {l: usd(v) for l, v in
                          ((st["step"], st["amount"]) for st in _wf_rows(a))},
        },
        "margin": {
            "hardware_gross_margin": usd(s["hw_margin"]), "hardware_gross_margin_pct": pct(s["hw_margin_pct"]),
            "license_gross_margin_pv": usd(s["lic_margin_pv"]), "license_gross_margin_pct": pct(s["lic_margin_pct"]),
            "blended_pv_margin": usd(s["margin_pv"]), "blended_pv_margin_pct": pct(s["margin_pv_pct"]),
            "margin_floor": pct(r.margin_floor), "discount_rate": pct(C.DISCOUNT_RATE),
        },
        "routing": {
            "required_tier": r.tier, "floor_check": r.status,
            "tier_limits": {k: pct(v) for k, v in r.thresholds.items()},
        },
        "benchmark": {
            "peer_count": b.n, "matched_on": f"{b.match}, nearest by size, product mix, country and channel",
            "peer_p25": pct(b.p25), "peer_median": pct(b.median), "peer_p75": pct(b.p75),
            "peer_p90": pct(b.p90), "position": b.status,
            "pv_margin_at_stake_vs_median": usd(b.margin_at_stake),
            "precedent_deals": precedents,
        },
        "options": [{
            "path": p.name, "customer_ask": p.customer_ask, "discount": pct(p.discount),
            "term": f"{p.term_years}-year", "list_value": usd(p.tcv_list), "pocket_price": usd(p.pocket_price),
            "pv_margin": usd(p.margin_pv), "pv_margin_pct": pct(p.margin_pv_pct),
            "approval_tier": p.tier, "floor_check": "PASS" if p.floor_pass else "FAIL",
        } for p in a.counters],
        "recommendation": {
            "path": rec.name, "customer_ask": rec.customer_ask, "discount": pct(rec.discount),
            "pv_margin": usd(rec.margin_pv), "pv_margin_pct": pct(rec.margin_pv_pct),
            "approval_tier": rec.tier,
            "pv_margin_change_vs_request": usd(rec.margin_pv - s["margin_pv"]),
        },
    }


def _wf_rows(a: Analysis):
    from .economics import waterfall
    return waterfall(a.deal).as_rows()


# --------------------------------------------------------------------------
# number guard
# --------------------------------------------------------------------------
_NUM = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?%?")


def _norm(tok: str) -> str:
    return tok.replace("$", "").replace(",", "").lstrip("-")


def allowed_numbers(facts: dict) -> set:
    return {_norm(t) for t in _NUM.findall(json.dumps(facts))}


def unverified_numbers(text: str, facts: dict) -> list:
    """Numbers in the memo that are not in FACTS. Small integers (counts,
    list markers) and year/term words are allowed."""
    ok = allowed_numbers(facts)
    bad = []
    for tok in _NUM.findall(text):
        n = _norm(tok)
        if n in ok or n.rstrip("%") in ok:
            continue
        bare = not tok.endswith("%") and "$" not in tok
        if bare and (re.fullmatch(r"\d{1,2}", n) or re.fullmatch(r"20\d\d", n)):
            continue
        bad.append(tok)
    return sorted(set(bad))


# --------------------------------------------------------------------------
# LLM drafting
# --------------------------------------------------------------------------
SYSTEM = """You write deal desk approval memos for a hardware and subscription \
networking vendor. The reader is a finance approver deciding a discount request.

Every figure you use must be copied exactly as written from the FACTS JSON. \
Do not calculate, round, convert, sum or estimate anything, and do not \
introduce any figure that is not in FACTS. If a comparison needs a number that \
is not in FACTS, describe it in words instead.

Write in Markdown with these sections:
1. A one-line header with deal ID, account, segment and the decision (Approve, \
Approve with counter, or Escalate).
2. **Recommendation**: the recommended path, what the customer is asked to \
give, and the approval tier it needs.
3. **Rationale**: why, using the pocket price waterfall, the blended PV margin \
(hardware vs license), the margin floor and the routing result. Call out \
where the margin goes, especially partner margin and hardware discount.
4. **Precedent**: the peer band and 3-5 named precedent deals by ID.
5. **Options considered**: a short table of the paths with discount, PV margin, \
PV margin %, tier and floor check.
6. **Approval**: who signs, and any condition attached.

Plain, direct prose. No preamble, no sign-off. Under 450 words."""


def draft_with_llm(facts: dict) -> str:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        messages=[{"role": "user", "content": "FACTS:\n" + json.dumps(facts, indent=2)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# --------------------------------------------------------------------------
# deterministic fallback
# --------------------------------------------------------------------------
def draft_template(facts: dict) -> str:
    rq, pr, mg, rt, bm, rec = (facts[k] for k in
                               ("request", "pricing", "margin", "routing", "benchmark", "recommendation"))
    if rec["path"] == "As requested":
        decision = "Approve"
    elif rec["approval_tier"] == "CFO":
        decision = "Escalate"
    else:
        decision = "Approve with counter"
    wf = pr["waterfall"]
    lines = [
        f"### {rq['deal_id']} | {rq['account']} | {rq['segment']} | {decision}",
        "",
        "**Recommendation**",
        f"{rec['path']}. Customer ask: {rec['customer_ask'].lower()}. Discount {rec['discount']}, "
        f"PV margin {rec['pv_margin']} ({rec['pv_margin_pct']}), approval tier {rec['approval_tier']}. "
        f"Change in PV margin vs. the request: {rec['pv_margin_change_vs_request']}.",
        "",
        "**Rationale**",
        f"The request is a {rq['term']} {rq['channel']} deal in {rq['geo']} at {pr['list_price']} list, asking "
        f"for {pr['requested_negotiated_discount']} negotiated discount ({pr['hardware_discount']} on hardware, "
        f"{pr['license_discount']} on license). Pocket price is {pr['pocket_price']}, "
        f"{pr['pocket_as_pct_of_list']} of list. Partner margin takes {wf['Partner margin']} and the hardware "
        f"discount takes {wf['Negotiated discount: hardware']}.",
        f"Blended PV margin is {mg['blended_pv_margin']} ({mg['blended_pv_margin_pct']}) against a "
        f"{mg['margin_floor']} floor: hardware {mg['hardware_gross_margin']} ({mg['hardware_gross_margin_pct']}), "
        f"license {mg['license_gross_margin_pv']} PV ({mg['license_gross_margin_pct']}). "
        f"Routing: {rt['required_tier']}, {rt['floor_check']}.",
        "",
        "**Precedent**",
        f"{bm['peer_count']} peers ({bm['matched_on']}). Band {bm['peer_p25']} to {bm['peer_p75']}, "
        f"median {bm['peer_median']}. The request is **{bm['position']}**; PV margin at stake vs. the median "
        f"is {bm['pv_margin_at_stake_vs_median']}.",
        "",
        "| Precedent | Quarter | Geo | Channel | Term | List | Discount | PV margin |",
        "|---|---|---|---|---|---|---|---|",
        *[f"| {p['deal_id']} | {p['quarter']} | {p['geo']} | {p['channel']} | {p['term']} | {p['list_value']} | "
          f"{p['negotiated_discount']} | {p['pv_margin']} |" for p in bm["precedent_deals"]],
        "",
        "**Options considered**",
        "",
        "| Path | Discount | PV margin | PV margin % | Tier | Floor |",
        "|---|---|---|---|---|---|",
        *[f"| {o['path']} | {o['discount']} | {o['pv_margin']} | {o['pv_margin_pct']} | {o['approval_tier']} | "
          f"{o['floor_check']} |" for o in facts["options"]],
        "",
        "**Approval**",
        f"{rec['approval_tier']} approval on the recommended path"
        + ("." if rec["path"] == "As requested" else f", conditional on: {rec['customer_ask'].lower()}."),
    ]
    return "\n".join(lines)


def draft(a: Analysis, use_llm: bool = True) -> dict:
    facts = build_facts(a)
    source, err = "template", None
    text = None
    if use_llm:
        try:
            text = draft_with_llm(facts)
            source = MODEL
        except Exception as e:  # no key, network, refusal: fall back, never block the desk
            err = f"{type(e).__name__}: {e}"
    if text is None:
        text = draft_template(facts)
    kind = "AI-drafted narrative" if source != "template" else "structured template"
    header = f"*Deal desk approval memo · {date.today():%Y-%m-%d} · {kind} · all figures from the pricing engine*\n\n"
    return {"markdown": header + text, "facts": facts, "source": source, "error": err,
            "unverified": unverified_numbers(text, facts)}
