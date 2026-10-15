from dataclasses import replace

import pytest

from dealdesk import config as C
from dealdesk import db, memo
from dealdesk.analysis import analyze
from dealdesk.economics import Deal, margin, negotiated_discount, summarize, waterfall
from dealdesk.routing import route

BASE = Deal(segment="Enterprise", geo="Germany", channel="Reseller", partner_tier="Gold", term_years=3,
            lines=(("SW-48", 40), ("AP-IN", 120)), hw_discount=0.30, lic_discount=0.15, payment_days=60,
            fx_realized=0.95)


@pytest.fixture(scope="session")
def con():
    db.ensure_book()
    c = db.connect()
    yield c
    c.close()


def test_waterfall_reconciles():
    wf = waterfall(BASE)
    steps = wf.steps
    assert steps[0][1] + sum(a for l, a, k in steps[1:4]) == pytest.approx(wf.invoice_price)
    assert wf.invoice_price + sum(a for l, a, k in steps[5:10]) == pytest.approx(wf.pocket_price)
    assert wf.hw_pocket + wf.lic_pocket == pytest.approx(wf.pocket_price)


def test_direct_has_no_partner_margin():
    wf = waterfall(replace(BASE, channel="Direct", partner_tier=None))
    assert dict((l, a) for l, a, k in wf.steps)["Partner margin"] == 0


def test_usd_deal_has_no_fx_variance():
    wf = waterfall(replace(BASE, geo="US", fx_realized=None))
    assert dict((l, a) for l, a, k in wf.steps)["FX variance"] == 0


def test_margin_falls_with_discount():
    ms = [margin(BASE.with_discount(x)).margin_pv for x in (0.0, 0.1, 0.2, 0.3, 0.4)]
    assert all(a > b for a, b in zip(ms, ms[1:]))


def test_with_discount_hits_target():
    assert negotiated_discount(BASE.with_discount(0.237)) == pytest.approx(0.237)


def test_pv_below_nominal_for_multiyear():
    m = margin(replace(BASE, term_years=5))
    assert m.lic_revenue_pv < m.lic_revenue_nominal


def test_routing_tiers_escalate_monotonically():
    tiers = [route(BASE.with_discount(x)).tier for x in (0.05, 0.20, 0.30, 0.45)]
    ranks = [C.TIERS.index(t) for t in tiers]
    assert ranks == sorted(ranks) and tiers[0] == "Rep" and tiers[-1] == "CFO"


def test_floor_failure_forces_cfo():
    deep = BASE.with_discount(0.50)
    r = route(deep)
    assert not r.floor_pass and r.tier == "CFO"


def test_counters_never_worse_than_hold(con):
    for i in ["REQ-03921", "REQ-03922", "REQ-03924"]:
        a = analyze(con, db.load_deal(con, i))
        hold = a.counters[1]
        for p in a.counters[2:]:
            assert p.margin_pv >= hold.margin_pv - 1
            assert p.discount <= a.summary["negotiated_discount"] + 1e-9
        assert a.recommended.floor_pass or a.recommended is hold


def test_peers_exclude_self_and_match_segment(con):
    d = db.load_deal(con, "D-00651")
    peers, _ = db.find_peers(con, d, summarize(d))
    assert "D-00651" not in set(peers.deal_id)
    assert set(peers.segment) == {d.segment}


def test_memo_template_has_no_unverified_numbers(con):
    a = analyze(con, db.load_deal(con, "REQ-03921"))
    m = memo.draft(a, use_llm=False)
    assert m["unverified"] == []
    assert "REQ-03921" in m["markdown"]


def test_number_guard_catches_invented_figures(con):
    a = analyze(con, db.load_deal(con, "REQ-03921"))
    facts = memo.build_facts(a)
    assert memo.unverified_numbers("Margin improves by $1,234,567 to 99.9%.", facts) == ["$1,234,567", "99.9%"]
