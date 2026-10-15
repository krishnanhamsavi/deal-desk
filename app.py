"""Deal Desk Console: margin and discount governance for hardware + subscription deals.

    streamlit run app.py
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dealdesk import config as C
from dealdesk import db, memo
from dealdesk.analysis import Analysis, analyze
from dealdesk.economics import Deal, summarize, waterfall

st.set_page_config(page_title="Deal Desk Console", page_icon=":material/request_quote:", layout="wide")

BLUE, ORANGE, AQUA, VIOLET, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7", "#8a8984"
RED, AMBER, GREEN = "#c4382f", "#b86e00", "#1a7f4b"
POSITION_COLOR = {"Outlier": RED, "Above band": AMBER, "In band": GREEN, "Below band": BLUE}
POSITIONS = ["Outlier", "Above band", "In band", "Below band"]

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; max-width: 1400px;}
  [data-testid="stMetricValue"] {font-size: 1.65rem;}
  .eyebrow {font-size: .78rem; letter-spacing: .08em; text-transform: uppercase; color: #6b7280; margin-bottom: -.4rem;}
  .step {border-left: 3px solid #2a78d6; padding: .2rem 0 .2rem .8rem; margin-bottom: .6rem;}
  .step b {display: block; margin-bottom: .15rem;}
  .step span {font-size: .88rem; color: #4b5563;}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def get_con():
    db.ensure_book()
    return db.connect()


con = get_con()
usd, pct = memo.usd, memo.pct


def money_short(x: float) -> str:
    a = abs(x)
    s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.1f}M" if a >= 1e6 else f"${a / 1e3:.0f}K"
    return "-" + s if x < 0 else s


def md(text: str) -> str:
    """Escape dollar signs so Streamlit markdown does not render them as LaTeX."""
    return text.replace("$", r"\$")


def page_header(eyebrow: str, title: str, blurb: str):
    st.markdown(f'<p class="eyebrow">{eyebrow}</p>', unsafe_allow_html=True)
    st.title(title)
    st.markdown(blurb)


def base_layout(fig, h=360):
    fig.update_layout(height=h, margin=dict(l=10, r=10, t=50, b=10), plot_bgcolor="rgba(0,0,0,0)",
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(size=12), title=dict(font=dict(size=14)),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=1, xanchor="right"))
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.18)", zeroline=False)
    return fig


def color_text(colors: dict):
    return lambda v: f"color: {colors[v]}; font-weight: 600" if v in colors else ""


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
SHORT = {"List price": "List", "Term discount": "Term<br>discount",
         "Negotiated discount: hardware": "Hardware<br>discount",
         "Negotiated discount: license": "License<br>discount", "Invoice price": "Invoice",
         "Partner margin": "Partner<br>margin", "Rebates & deal reg": "Rebates &<br>deal reg",
         "Payment terms": "Payment<br>terms", "Freight & duty": "Freight<br>& duty", "FX variance": "FX<br>variance",
         "Pocket price": "Pocket"}


def waterfall_chart(rows: list[dict], title="Pocket price waterfall", short_money=False):
    fmt = money_short if short_money else usd
    fig = go.Figure(go.Waterfall(
        x=[SHORT.get(r["step"], r["step"]) for r in rows], y=[r["amount"] for r in rows],
        measure=["absolute" if r["kind"] == "total" else "relative" for r in rows],
        text=[fmt(r["amount"]) for r in rows], textposition="outside", cliponaxis=False,
        totals=dict(marker=dict(color=BLUE)), decreasing=dict(marker=dict(color=ORANGE)),
        increasing=dict(marker=dict(color=AQUA)), connector=dict(line=dict(color=GRAY, width=1)),
        customdata=[r["step"] for r in rows], hovertemplate="%{customdata}<br>%{y:$,.0f}<extra></extra>"))
    fig.update_layout(title=title, showlegend=False)
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    return base_layout(fig, 420)


def peer_chart(a: Analysis):
    b = a.band
    p = b.peers
    fig = go.Figure()
    fig.add_vrect(x0=b.p25, x1=b.p75, fillcolor=BLUE, opacity=0.10, line_width=0,
                  annotation_text="Typical range (P25–P75)", annotation_position="top left")
    fig.add_vline(x=b.median, line=dict(color=BLUE, dash="dot", width=2),
                  annotation_text=f"Median {pct(b.median)}", annotation_position="bottom right")
    fig.add_vline(x=b.p90, line=dict(color=GRAY, dash="dash", width=1),
                  annotation_text=f"Outlier line (P90) {pct(b.p90)}", annotation_position="top right")
    jitter = np.random.default_rng(0).uniform(-0.3, 0.3, len(p))
    fig.add_trace(go.Scatter(
        x=p.negotiated_discount, y=jitter, mode="markers", name="Comparable closed deals",
        marker=dict(size=10, color=BLUE, opacity=0.75, line=dict(color="white", width=1.5)),
        customdata=np.c_[p.deal_id, p.close_quarter, p.geo, p.list_price, p.margin_pv_pct],
        hovertemplate="%{customdata[0]} · %{customdata[1]} · %{customdata[2]}<br>discount %{x:.1%}"
                      "<br>TCV $%{customdata[3]:,.0f}<br>PV margin %{customdata[4]:.1%}<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=[a.summary["negotiated_discount"]], y=[0], mode="markers+text", name="This request",
        marker=dict(size=18, color=ORANGE, symbol="diamond", line=dict(color="white", width=2)),
        text=[f"Request {pct(a.summary['negotiated_discount'])}"], textposition="top center",
        hovertemplate="This request<br>discount %{x:.1%}<extra></extra>"))
    fig.update_yaxes(visible=False, range=[-1, 1])
    fig.update_xaxes(tickformat=".0%", title="Negotiated discount off standard price")
    fig.update_layout(title=f"Discount vs. {b.n} comparable closed deals ({b.match})")
    return base_layout(fig, 320)


def paths_chart(a: Analysis):
    df = pd.DataFrame([p.as_row() for p in a.counters])
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df.name, y=df.revenue_pv, name="Revenue (PV, pocket)", marker_color=BLUE,
                         hovertemplate="%{x}<br>Revenue %{y:$,.0f}<extra></extra>"))
    fig.add_trace(go.Bar(x=df.name, y=df.margin_pv, name="Gross margin (PV)", marker_color=AQUA,
                         text=[pct(v) for v in df.margin_pv_pct], textposition="outside", cliponaxis=False,
                         hovertemplate="%{x}<br>Gross margin %{y:$,.0f}<extra></extra>"))
    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08,
                      title="Revenue and margin outcome of each path (label = PV margin %)")
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    return base_layout(fig, 380)


def margin_split_chart(s: dict):
    vals = [(s["hw_margin"], s["hw_margin_pct"]), (s["lic_margin_pv"], s["lic_margin_pct"]),
            (s["margin_pv"], s["margin_pv_pct"])]
    fig = go.Figure(go.Bar(
        x=["Hardware<br>(booked at signing)", "License<br>(PV over term)", "Blended deal"],
        y=[v for v, _ in vals], marker_color=[ORANGE if s["hw_margin"] < 0 else BLUE, AQUA, VIOLET],
        text=[f"{usd(v)}<br>{pct(p)}" for v, p in vals], textposition="outside", cliponaxis=False,
        hovertemplate="%{x}<br>%{y:$,.0f}<extra></extra>"))
    fig.update_layout(title=f"Gross margin by component, discounted at {pct(C.DISCOUNT_RATE)}", showlegend=False)
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    return base_layout(fig, 360)


# --------------------------------------------------------------------------
# decision brief + read-out
# --------------------------------------------------------------------------
def decision_brief(a: Analysis):
    s, r, b, rec = a.summary, a.route, a.band, a.recommended
    if rec.name == "As requested":
        verdict, show = "Approve as requested", st.success
    elif not rec.floor_pass:
        verdict, show = "Escalate: no path clears the margin floor", st.error
    else:
        verdict, show = f"Counter: {rec.name}", st.warning
    uplift = rec.margin_pv - s["margin_pv"]
    lines = [
        f"**Discount:** {pct(s['negotiated_discount'])} requested vs. a peer median of {pct(b.median)} "
        f"across {b.n} comparable deals, so the request is **{b.status.lower()}**.",
        f"**Margin:** blended PV margin of {pct(s['margin_pv_pct'])} against a {pct(r.margin_floor)} floor "
        f"({'clears' if r.floor_pass else 'breaches'} it). Hardware alone runs at {pct(s['hw_margin_pct'])}.",
        f"**Approval:** as submitted, this routes to **{r.tier}**.",
    ]
    if rec.name != "As requested":
        lines.append(f"**Ask:** {rec.customer_ask}. At {pct(rec.discount)} this earns {usd(rec.margin_pv)} PV margin "
                     f"({'+' if uplift >= 0 else ''}{usd(uplift)} vs. as submitted) and needs **{rec.tier}** sign-off.")
    show(md(f"#### {verdict}\n" + "\n".join(f"- {x}" for x in lines)))


def render_analysis(a: Analysis):
    s, r, b = a.summary, a.route, a.band
    k = st.columns(5)
    k[0].metric("Contract value (list)", usd(s["list_price"]), s["size_band"], delta_color="off", delta_arrow="off")
    k[1].metric("Pocket price", usd(s["pocket_price"]), f"{pct(s['pocket_pct_of_list'])} of list",
                delta_color="off", delta_arrow="off")
    k[2].metric("Blended PV margin", pct(s["margin_pv_pct"]), f"floor {pct(r.margin_floor)}",
                delta_color="normal" if r.floor_pass else "inverse", delta_arrow="off")
    k[3].metric("Approver", r.tier, "within floor" if r.floor_pass else "below floor",
                delta_color="off", delta_arrow="off")
    k[4].metric("Margin at stake", usd(b.margin_at_stake), f"vs. peer median · {b.status.lower()}",
                delta_color="off", delta_arrow="off")

    t1, t2, t3, t4 = st.tabs([":material/waterfall_chart: Price waterfall", ":material/donut_large: Blended margin",
                              ":material/group: Peer benchmark", ":material/handshake: Counter-offers"])
    with t1:
        st.caption("Where the list price goes before the company keeps it. On-invoice items are visible to the "
                   "customer; off-invoice items (partner margin, rebates, terms, freight, FX) are not, which is "
                   "why they leak.")
        rows = waterfall(a.deal).as_rows()
        st.plotly_chart(waterfall_chart(rows), width="stretch")
        wf = pd.DataFrame(rows).rename(columns={"step": "Step", "amount": "Amount", "kind": "Type"})
        wf["% of list"] = (wf.Amount / s["list_price"]).round(4) + 0.0
        wf["Type"] = wf.Type.str.replace("total", "Subtotal").str.capitalize()
        st.dataframe(wf.style.format({"Amount": usd, "% of list": "{:.1%}"}), hide_index=True, width="stretch")
    with t2:
        c1, c2 = st.columns([3, 2])
        c1.plotly_chart(margin_split_chart(s), width="stretch")
        with c2:
            st.markdown("##### The term trade")
            st.markdown("Reps often justify a deep hardware discount with a long license term. The table shows the "
                        "highest hardware discount each term can carry while earning the same PV margin as this "
                        "request. If the requested hardware discount is above break-even for the term on the "
                        "table, the license tail does not pay it back.")
            tt = pd.DataFrame(a.term_trade)
            tt.insert(0, "Term", [f"{t} yr" + ("  ◀ requested" if t == a.deal.term_years else "")
                                  for t in tt.term_years])
            tt = tt.drop(columns="term_years").rename(columns={
                "margin_pv_at_requested": "PV margin at request", "margin_pv_pct_at_requested": "PV margin %",
                "breakeven_hw_discount": "Break-even hardware discount"})
            st.dataframe(tt.style.format({"PV margin at request": usd, "PV margin %": "{:.1%}",
                                          "Break-even hardware discount":
                                              lambda v: "n/a" if pd.isna(v) else f"{v:.1%}"}),
                         hide_index=True, width="stretch")
            st.caption(f"Requested: **{pct(a.deal.hw_discount)}** off hardware, **{pct(a.deal.lic_discount)}** off "
                       f"license on top of the published {pct(C.TERM_DISCOUNT[a.deal.term_years])} "
                       f"{a.deal.term_years}-year term discount.")
    with t3:
        st.caption("Comparable closed-won deals: same segment and region, nearest on deal size, hardware/license "
                   "mix, primary product line, country and channel.")
        st.plotly_chart(peer_chart(a), width="stretch")
        pe = b.peers.rename(columns={
            "deal_id": "Deal", "account": "Account", "rep": "Rep", "geo": "Country", "channel": "Channel",
            "partner_tier": "Partner tier", "term_years": "Term (yr)", "close_quarter": "Closed",
            "list_price": "TCV (list)", "hw_share": "Hardware share", "primary_line": "Primary line",
            "negotiated_discount": "Discount", "margin_pv_pct": "PV margin", "pocket_pct_of_list": "Pocket / list",
            "distance": "Match distance"}).drop(columns=["segment", "region"])
        st.dataframe(pe.style.format({"TCV (list)": "${:,.0f}", "Hardware share": "{:.0%}", "Discount": "{:.1%}",
                                      "PV margin": "{:.1%}", "Pocket / list": "{:.1%}", "Match distance": "{:.2f}",
                                      "Partner tier": lambda v: v or "—"}),
                     hide_index=True, width="stretch", height=300)
    with t4:
        st.caption("Instead of approve or deny, each path trades discount for something of value. Discounts are "
                   "solved so every trade earns at least the price-only counter, clears the margin floor, and stays "
                   "within VP authority.")
        st.plotly_chart(paths_chart(a), width="stretch")
        df = pd.DataFrame([p.as_row() for p in a.counters])
        df["Recommended"] = np.where(df.name == a.recommended.name, "★", "")
        df["floor_pass"] = df.floor_pass.map({True: "Pass", False: "Fail"})
        df = df[["Recommended", "name", "customer_ask", "discount", "term_years", "tcv_list", "pocket_price",
                 "revenue_pv", "margin_pv", "margin_pv_pct", "tier", "floor_pass"]].rename(columns={
                     "name": "Path", "customer_ask": "Customer gives", "discount": "Discount",
                     "term_years": "Term (yr)", "tcv_list": "TCV (list)", "pocket_price": "Pocket price",
                     "revenue_pv": "Revenue (PV)", "margin_pv": "Margin (PV)", "margin_pv_pct": "Margin %",
                     "tier": "Approver", "floor_pass": "Floor"})
        st.dataframe(df.style.format({"Discount": "{:.1%}", "TCV (list)": "${:,.0f}", "Pocket price": "${:,.0f}",
                                      "Revenue (PV)": "${:,.0f}", "Margin (PV)": "${:,.0f}", "Margin %": "{:.1%}"})
                     .map(color_text({"Pass": GREEN, "Fail": RED}), subset=["Floor"]),
                     hide_index=True, width="stretch")
    with st.expander("Routing detail"):
        for reason in r.reasons:
            st.markdown("- " + reason)
        st.markdown("Approval limits for this segment and size band: " + ", ".join(
            f"{t} up to {pct(v)}" for t, v in r.thresholds.items()) + "; CFO above.")


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------
def overview_view():
    page_header("Revenue operations · pricing governance", "Deal Desk Console",
                "A deal desk sits between sales and finance. Reps ask for discounts to win deals; the desk decides "
                "what the company can afford to give and what it should ask for in return. This console runs that "
                "process for a vendor selling **networking hardware with multi-year subscription licenses** through "
                "direct and partner channels.")
    h = db.headline(con)
    q = pending_queue()
    k = st.columns(5)
    k[0].metric("Closed deals analysed", f"{h['n_deals']:,}", "2023Q1–2026Q3", delta_color="off", delta_arrow="off")
    k[1].metric("List value booked", money_short(h["list"]))
    k[2].metric("Realised as pocket price", pct(h["pocket_pct"]), "of list", delta_color="off", delta_arrow="off")
    k[3].metric("Margin leakage (PV)", money_short(h["total_leakage"]), "beyond peer norms",
                delta_color="off", delta_arrow="off")
    k[4].metric("Requests awaiting review", len(q), f"{money_short(q["Margin at stake"].sum())} margin at stake",
                delta_color="off", delta_arrow="off")

    st.markdown("### The problem")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**Headline discount hides the real price.** After partner margin, rebates, payment terms, freight, "
                "duty and FX, the company keeps far less than the invoice suggests. Most of that erosion never "
                "appears on the quote.")
    c2.markdown("**Hardware and license margins get traded.** Reps give away hardware margin up front and point to a "
                "long license term to justify it. Whether the license tail actually pays it back depends on term, "
                "cost of capital and license discount.")
    c3.markdown("**Approve or deny is the wrong question.** A good desk counters: hold the discount if the customer "
                "extends the term, commits to volume or adds a product line, and shows what each path is worth.")

    st.markdown("### How a request is evaluated")
    steps = [("1 · Pocket price waterfall", "List price down through every on- and off-invoice deduction to what "
              "the company actually keeps."),
             ("2 · Blended PV margin", "Hardware margin at signing plus license margin over the full term, "
              "discounted to present value."),
             ("3 · Approval routing", "Segment × deal-size limits pick Rep, Manager, VP or CFO. Below the margin "
              "floor is a CFO exception."),
             ("4 · Peer benchmark", "The 30 most comparable closed deals set the normal range and price the margin "
              "at stake."),
             ("5 · Counter-offer & memo", "Discount-for-value trades, a recommended path, and an approval memo with "
              "precedent deals cited.")]
    cols = st.columns(5)
    for col, (t, d) in zip(cols, steps):
        col.markdown(f'<div class="step"><b>{t}</b><span>{d}</span></div>', unsafe_allow_html=True)

    st.markdown("### Where to start")
    c1, c2, c3 = st.columns(3)
    with c1.container(border=True):
        st.markdown("**:material/inbox: Desk queue**  \nWork pending requests in order of margin at stake, review the "
                    "evidence, draft the memo and record a decision.")
    with c2.container(border=True):
        st.markdown("**:material/edit_note: New request**  \nBuild a quote as a rep and see how the desk will read it "
                    "before submitting.")
    with c3.container(border=True):
        st.markdown("**:material/query_stats: Leakage analytics**  \nFind where margin leaks across the book: by "
                    "channel, discount level, segment, rep and quarter.")

    with st.expander("Glossary"):
        st.markdown("""
| Term | Meaning |
|---|---|
| **Standard price** | List price less the published multi-year license discount. Negotiated discount starts here. |
| **Invoice price** | What the customer is billed after all on-invoice discounts. |
| **Pocket price** | What the company keeps after partner margin, rebates, payment-terms cost, freight, duty and FX. |
| **Blended PV margin** | Hardware gross margin plus license gross margin over the term, at present value. |
| **Margin floor** | Minimum blended PV margin by segment and channel. Below it, only the CFO can approve. |
| **Peer band** | The P25–P75 discount range of the 30 most comparable closed deals. Above P90 is an outlier. |
| **Margin at stake** | PV margin given up by pricing at the request instead of the peer median. |
| **Leakage** | For closed deals priced above their peer P75: PV margin lost vs. pricing at P75. |
""")
    st.caption("All deal data is synthetic, generated from documented distributions. No real customer or company data.")


# --------------------------------------------------------------------------
# rep view
# --------------------------------------------------------------------------
def rep_view():
    page_header("Sales", "New discount request",
                "Configure the quote. The desk's read-out updates live, so you can see the approver, the peer "
                "benchmark and the likely counter before you submit.")
    with st.container(border=True):
        st.markdown("**Deal terms**")
        c = st.columns(4)
        segment = c[0].selectbox("Segment", C.SEGMENTS)
        geo = c[1].selectbox("Country", list(C.GEOS))
        channel = c[2].selectbox("Channel", ["Reseller", "Direct"])
        tier = c[3].selectbox("Partner tier", list(C.PARTNER_TIERS), disabled=channel == "Direct")
        c = st.columns(4)
        term = c[0].selectbox("License term", C.TERMS, index=1, format_func=lambda t: f"{t} year" + "s" * (t > 1))
        pay = c[1].selectbox("Payment terms", C.PAYMENT_TERMS_DAYS, format_func=lambda d: f"Net {d}")
        hw_d = c[2].slider("Hardware discount", 0, 70, 30, 1, format="%d%%") / 100
        lic_d = c[3].slider("License discount (beyond term discount)", 0, 60, 15, 1, format="%d%%") / 100

        st.markdown("**Bill of materials**")
        bom = pd.DataFrame([{"SKU": s, "Product line": v[0], "Description": v[1], "Hardware list ($)": v[2],
                             "License list ($/yr)": v[4],
                             "Quantity": {"SW-48": 40, "AP-IN": 120, "EDGE-L": 2}.get(s, 0)}
                            for s, v in C.SKUS.items()])
        bom = st.data_editor(bom, hide_index=True, width="stretch",
                             disabled=["SKU", "Product line", "Description", "Hardware list ($)",
                                       "License list ($/yr)"],
                             column_config={"Quantity": st.column_config.NumberColumn(min_value=0, step=1),
                                            "Hardware list ($)": st.column_config.NumberColumn(format="$%d"),
                                            "License list ($/yr)": st.column_config.NumberColumn(format="$%d")})
    lines = tuple((r.SKU, int(r.Quantity)) for r in bom.itertuples() if r.Quantity and r.Quantity > 0)
    if not lines:
        st.info("Add at least one SKU to the bill of materials.")
        return
    d = Deal(segment=segment, geo=geo, channel=channel, partner_tier=tier if channel == "Reseller" else None,
             term_years=term, lines=lines, hw_discount=hw_d, lic_discount=lic_d, payment_days=pay,
             deal_id="DRAFT", rep="R001", close_quarter="2026Q4", account="New account")
    a = analyze(con, d)
    st.markdown("### Desk preview")
    decision_brief(a)
    render_analysis(a)

    st.markdown("### Submit")
    with st.form("submit", border=True):
        c = st.columns([2, 2, 1])
        rep = c[0].text_input("Rep ID", "R001")
        acct = c[1].text_input("Account", "ACCT-NEW")
        c[2].markdown("&nbsp;")
        if c[2].form_submit_button("Submit to deal desk", type="primary", width="stretch"):
            n = con.execute("SELECT count(*) FROM deals").fetchone()[0] + 1
            d2 = replace(d, deal_id=f"REQ-{n:05d}", rep=rep, account=acct)
            row = {"deal_id": d2.deal_id, "status": "Pending", "account": acct, "rep": rep, "segment": segment,
                   "geo": geo, "region": C.GEOS[geo][0], "currency": C.GEOS[geo][1], "channel": channel,
                   "partner_tier": d2.partner_tier, "term_years": term, "payment_days": pay,
                   "hw_discount": hw_d, "lic_discount": lic_d, "fx_realized": C.GEOS[geo][2],
                   "close_quarter": "2026Q4", **summarize(d2)}
            db.insert_request(con, row, lines)
            pending_queue.clear()
            st.success(f"Submitted **{d2.deal_id}**. It is now in the desk queue.")


# --------------------------------------------------------------------------
# desk queue
# --------------------------------------------------------------------------
@st.cache_data(show_spinner="Benchmarking pending requests…")
def pending_queue() -> pd.DataFrame:
    ids = [r[0] for r in con.execute("SELECT deal_id FROM deals WHERE status='Pending' ORDER BY deal_id").fetchall()]
    rows = []
    for i in ids:
        a = analyze(con, db.load_deal(con, i))
        rows.append({"Deal": i, "Account": a.deal.account, "Rep": a.deal.rep, "Segment": a.deal.segment,
                     "Country": a.deal.geo, "Channel": a.deal.channel, "Term (yr)": a.deal.term_years,
                     "TCV (list)": a.summary["list_price"], "Discount": a.summary["negotiated_discount"],
                     "Peer median": a.band.median, "Position": a.band.status,
                     "PV margin": a.summary["margin_pv_pct"], "Approver": a.route.tier,
                     "Floor": "Pass" if a.route.floor_pass else "Fail",
                     "Margin at stake": a.band.margin_at_stake, "Recommendation": a.recommended.name})
    cols = ["Deal", "Account", "Rep", "Segment", "Country", "Channel", "Term (yr)", "TCV (list)", "Discount",
            "Peer median", "Position", "PV margin", "Approver", "Floor", "Margin at stake", "Recommendation"]
    return pd.DataFrame(rows, columns=cols).sort_values("Margin at stake", ascending=False)


def desk_view():
    page_header("Deal desk", "Review queue",
                "Pending discount requests, ranked by the PV margin the company would give up versus comparable "
                "deals. Select a request to see the evidence, the recommended counter and the approval memo.")
    q = pending_queue()
    if q.empty:
        st.info("The queue is empty. Submit a request from **New request**.")
        return
    k = st.columns(4)
    k[0].metric("Pending requests", len(q))
    k[1].metric("Need CFO sign-off", int((q.Approver == "CFO").sum()))
    k[2].metric("Below margin floor", int((q.Floor == "Fail").sum()))
    k[3].metric("Margin at stake (PV)", usd(q["Margin at stake"].sum()))

    f = st.columns(3)
    seg = f[0].multiselect("Segment", C.SEGMENTS, placeholder="All segments")
    tiers = f[1].multiselect("Approver", C.TIERS, placeholder="All approvers")
    pos = f[2].multiselect("Peer position", POSITIONS, placeholder="All positions")
    view = q[q.Segment.isin(seg or C.SEGMENTS) & q.Approver.isin(tiers or C.TIERS) & q.Position.isin(pos or POSITIONS)]
    if view.empty:
        st.info("No requests match the filters.")
        return
    sel = st.dataframe(
        view.style.format({"TCV (list)": "${:,.0f}", "Discount": "{:.1%}", "Peer median": "{:.1%}",
                           "PV margin": "{:.1%}", "Margin at stake": "${:,.0f}"})
        .map(color_text(POSITION_COLOR), subset=["Position"])
        .map(color_text({"Pass": GREEN, "Fail": RED}), subset=["Floor"]),
        hide_index=True, width="stretch", height=330, on_select="rerun", selection_mode="single-row")
    rows = sel.selection.rows
    deal_id = view.iloc[rows[0]].Deal if rows else view.iloc[0].Deal
    if not rows:
        st.caption("Showing the top request. Select any row to open it.")

    d = db.load_deal(con, deal_id)
    a = analyze(con, d)
    st.divider()
    partner = f" via {d.partner_tier} partner" if d.partner_tier else ""
    st.markdown(f'<p class="eyebrow">{deal_id} · rep {d.rep} · {d.close_quarter}</p>', unsafe_allow_html=True)
    st.subheader(f"{d.account} — {d.segment}, {d.geo}, {d.channel.lower()}{partner}, {d.term_years}-year term")
    decision_brief(a)
    render_analysis(a)

    st.markdown("### Approval memo")
    st.caption("The memo narrative is AI-drafted from the engine's figures. The model is given every number and "
               "is not allowed to calculate; each figure in the draft is checked against the engine output.")
    c = st.columns([1.3, 1, 4])
    use_llm = c[0].toggle("AI-drafted narrative", value=True,
                          help="Without an API key, a structured template memo is produced from the same figures.")
    if c[1].button("Draft memo", type="primary", icon=":material/description:"):
        with st.spinner("Drafting memo…"):
            st.session_state[f"memo_{deal_id}"] = memo.draft(a, use_llm=use_llm)
    m = st.session_state.get(f"memo_{deal_id}")
    if m:
        if m["error"]:
            st.info("AI drafting unavailable; showing the template memo.")
        if m["unverified"]:
            st.error("Figures in the draft not found in the engine output: " + ", ".join(m["unverified"]))
        else:
            st.caption(":material/verified: Every figure in this memo traces to the pricing engine.")
        with st.container(border=True):
            st.markdown(md(m["markdown"]))
        c = st.columns([1, 1, 3])
        c[0].download_button("Download memo (.md)", m["markdown"], f"{deal_id}_memo.md", "text/markdown",
                             icon=":material/download:")
        c[1].download_button("Download figures (.json)", json.dumps(m["facts"], indent=2), f"{deal_id}_facts.json",
                             "application/json", icon=":material/data_object:")

    st.markdown("### Decision")
    with st.container(border=True):
        c = st.columns([2, 3, 1])
        choice = c[0].selectbox("Decision", [f"Approve counter: {a.recommended.name}", "Approve as requested",
                                             "Reject"])
        note = c[1].text_input("Note for the rep")
        c[2].markdown("&nbsp;")
        if c[2].button("Record", type="primary", width="stretch"):
            path = a.recommended if choice.startswith("Approve counter") else a.counters[0]
            con.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                        [deal_id, datetime.now(), choice, None if choice == "Reject" else path.discount,
                         path.name, note])
            con.execute("UPDATE deals SET status = ? WHERE deal_id = ?",
                        ["Rejected" if choice == "Reject" else "Approved", deal_id])
            pending_queue.clear()
            st.toast(f"Recorded decision for {deal_id}.")
            st.rerun()
    log = con.execute("SELECT deal_id AS Deal, decided_at AS \"Decided\", decision AS Decision, "
                      "approved_discount AS \"Approved discount\", note AS Note FROM decisions "
                      "ORDER BY decided_at DESC").df()
    if not log.empty:
        with st.expander(f"Decision log ({len(log)})"):
            st.dataframe(log.style.format({"Approved discount": lambda v: "—" if pd.isna(v) else f"{v:.1%}",
                                           "Decided": lambda v: f"{v:%Y-%m-%d %H:%M}"}),
                         hide_index=True, width="stretch")


# --------------------------------------------------------------------------
# leakage analytics
# --------------------------------------------------------------------------
def analytics_view():
    page_header("Finance", "Margin leakage across the deal book",
                "Leakage is the PV margin lost on closed deals that were discounted above their peer norm (P75), "
                "measured against pricing at P75. It isolates discount that comparable customers did not need.")
    h = db.headline(con)
    k = st.columns(4)
    k[0].metric("Closed-won deals", f"{h['n_deals']:,}")
    k[1].metric("Pocket price / list", pct(h["pocket_pct"]))
    k[2].metric("Margin leakage (PV)", usd(h["total_leakage"]))
    k[3].metric("Share from reseller deals at 30%+ discount", pct(h["reseller_30_share"]),
                f"{pct(h['reseller_30_deal_share'])} of deals", delta_color="off", delta_arrow="off")

    seg = db.leakage_by(con, ["segment"]).sort_values("margin_leakage")
    tierdf = db.leakage_by(con, ["channel", "partner_tier"])
    reps = db.leakage_by(con, ["rep"]).sort_values("margin_leakage", ascending=False)
    top_seg = seg.iloc[-1]
    top5_share = reps.head(5).margin_leakage.sum() / h["total_leakage"]
    with st.container(border=True):
        st.markdown("**Key findings**")
        st.markdown(md(
            f"- **Leakage is concentrated, not spread evenly.** Reseller deals discounted 30% or more are "
            f"{pct(h['reseller_30_deal_share'])} of deals but {pct(h['reseller_30_share'])} of leakage: partner "
            "margin and rebates are charged on top of an already deep discount.\n"
            f"- **{top_seg.segment} carries the most leaked margin** at {usd(top_seg.margin_leakage)}, "
            f"{pct(top_seg.margin_leakage / h['total_leakage'])} of the total.\n"
            f"- **A few reps drive a large share.** The top five reps account for {pct(top5_share)} of leakage; "
            "rep-level coaching and tighter manager limits are the cheapest fix.\n"
            "- **Recommended controls:** require a counter-offer above P75 of peers, and route reseller deals above "
            "30% to VP regardless of size."))

    wf = db.portfolio_waterfall(con).iloc[0]
    rows = [{"step": "List price", "amount": wf.list_price, "kind": "total"},
            {"step": "Term discount", "amount": wf.wf_term_discount, "kind": "x"},
            {"step": "Negotiated discount: hardware", "amount": wf.wf_neg_hardware, "kind": "x"},
            {"step": "Negotiated discount: license", "amount": wf.wf_neg_license, "kind": "x"},
            {"step": "Invoice price", "amount": wf.invoice_price, "kind": "total"},
            {"step": "Partner margin", "amount": wf.wf_partner_margin, "kind": "x"},
            {"step": "Rebates & deal reg", "amount": wf.wf_rebates_deal_reg, "kind": "x"},
            {"step": "Payment terms", "amount": wf.wf_payment_terms, "kind": "x"},
            {"step": "Freight & duty", "amount": wf.wf_freight_duty, "kind": "x"},
            {"step": "FX variance", "amount": wf.wf_fx_variance, "kind": "x"},
            {"step": "Pocket price", "amount": wf.pocket_price, "kind": "total"}]
    st.plotly_chart(waterfall_chart(rows, "Book-level pocket price waterfall, closed-won 2023Q1–2026Q3",
                                    short_money=True), width="stretch")

    c1, c2 = st.columns(2)
    lb = db.leakage_by(con, ["channel", "discount_bucket"])
    fig = go.Figure()
    for ch, col in (("Direct", BLUE), ("Reseller", ORANGE)):
        sub = lb[lb.channel == ch]
        fig.add_trace(go.Bar(x=sub.discount_bucket, y=sub.margin_leakage, name=ch, marker_color=col,
                             customdata=np.c_[sub.deals, sub.above_band],
                             hovertemplate=ch + " · %{x}<br>Leakage %{y:$,.0f}<br>%{customdata[0]} deals, "
                                                "%{customdata[1]} above peer band<extra></extra>"))
    fig.update_layout(barmode="group", title="Leakage by channel and discount level")
    fig.update_xaxes(title="Negotiated discount")
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    c1.plotly_chart(base_layout(fig), width="stretch")

    fig = go.Figure(go.Bar(y=seg.segment, x=seg.margin_leakage, orientation="h", marker_color=BLUE,
                           text=[money_short(v) for v in seg.margin_leakage], textposition="outside",
                           cliponaxis=False, hovertemplate="%{y}<br>%{x:$,.0f}<extra></extra>"))
    fig.update_layout(title="Leakage by segment")
    fig.update_xaxes(tickprefix="$", tickformat="~s", range=[0, seg.margin_leakage.max() * 1.2])
    c2.plotly_chart(base_layout(fig), width="stretch")

    c1, c2 = st.columns(2)
    tierdf["label"] = np.where(tierdf.channel == "Direct", "Direct", tierdf.partner_tier.fillna("") + " partner")
    tierdf["per_deal"] = tierdf.margin_leakage / tierdf.deals
    tierdf = tierdf.sort_values("per_deal")
    fig = go.Figure(go.Bar(y=tierdf.label, x=tierdf.per_deal, orientation="h", marker_color=ORANGE,
                           text=[usd(v) for v in tierdf.per_deal], textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}<br>%{x:$,.0f} per deal<extra></extra>"))
    fig.update_layout(title="Leakage per deal by route to market")
    fig.update_xaxes(tickprefix="$", tickformat="~s", range=[0, tierdf.per_deal.max() * 1.25])
    c1.plotly_chart(base_layout(fig, 340), width="stretch")

    qd = db.leakage_by(con, ["close_quarter"])
    is_q4 = qd.close_quarter.str.endswith("Q4")
    fig = go.Figure(go.Bar(x=qd.close_quarter, y=qd.margin_leakage, marker_color=np.where(is_q4, ORANGE, BLUE),
                           hovertemplate="%{x}<br>%{y:$,.0f}<extra></extra>"))
    fig.update_layout(title="Leakage by close quarter (Q4 highlighted: year-end push)")
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    c2.plotly_chart(base_layout(fig, 340), width="stretch")

    st.markdown("##### Reps with the most leakage")
    r = reps.head(12).rename(columns={"rep": "Rep", "deals": "Deals", "above_band": "Above peer band",
                                      "avg_discount": "Avg discount", "margin_leakage": "Leakage (PV)",
                                      "list_price": "TCV (list)"})
    r["Leakage / deal"] = r["Leakage (PV)"] / r.Deals
    st.dataframe(r[["Rep", "Deals", "Above peer band", "Avg discount", "TCV (list)", "Leakage (PV)",
                    "Leakage / deal"]].style.format({"Avg discount": "{:.1%}", "TCV (list)": "${:,.0f}",
                                                     "Leakage (PV)": "${:,.0f}", "Leakage / deal": "${:,.0f}"}),
                 hide_index=True, width="stretch")


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------
def policy_view():
    page_header("Governance", "Pricing policy",
                "The rules every request is evaluated against. Thresholds are maximum negotiated discounts off "
                "standard price; margin floors apply to blended PV margin.")
    st.markdown("##### Approval matrix")
    m = pd.DataFrame([{"Segment": s, "Deal size (TCV)": b, "Rep": v[0], "Manager": v[1], "VP": v[2],
                       "CFO": "Above VP"} for s, bands in C.APPROVAL_MATRIX.items() for b, v in bands.items()])
    st.dataframe(m.style.format({"Rep": "up to {:.0%}", "Manager": "up to {:.0%}", "VP": "up to {:.0%}"}),
                 hide_index=True, width="stretch", height=35 * (len(m) + 1) + 3)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Margin floors (blended PV margin)")
        fl = pd.DataFrame([{"Segment": s, "Direct": v[0], "Reseller": v[1]} for s, v in C.MARGIN_FLOOR.items()])
        st.dataframe(fl.style.format({"Direct": "{:.0%}", "Reseller": "{:.0%}"}), hide_index=True, width="stretch")
        st.caption("Reseller floors are lower because the partner carries pre-sales, delivery and first-line "
                   "support that a direct deal carries in operating cost.")
        st.markdown("##### Published term discounts (license)")
        st.dataframe(pd.DataFrame([{"Term": f"{t} year" + "s" * (t > 1), "Discount off license list": v}
                                   for t, v in C.TERM_DISCOUNT.items()])
                     .style.format({"Discount off license list": "{:.0%}"}), hide_index=True, width="stretch")
    with c2:
        st.markdown("##### Partner program")
        pp = pd.DataFrame([{"Tier": t, "Distributor": v[0], "Reseller": v[1],
                            "Deal reg rebate": v[2], "Volume rebate": v[3]}
                           for t, v in C.PARTNER_TIERS.items()])
        st.dataframe(pp.style.format({c: "{:.0%}" for c in pp.columns[1:]}), hide_index=True, width="stretch")
        st.markdown("##### Counter-offer levers")
        st.markdown(f"- **Term:** move to a 5-year term (or 10-year from 5).\n"
                    f"- **Volume:** customer commits to {C.VOLUME_COMMIT_UPLIFT:.0%} more units.\n"
                    f"- **Attach:** add a second product line sized at {C.ATTACH_LICENSE_RATIO:.0%} of the main "
                    "unit count.\n"
                    "- Each trade's discount is capped at VP authority and must earn at least the price-only "
                    "counter.")
        st.markdown("##### Financial assumptions")
        st.markdown(f"- Cost of capital for PV and payment terms: **{pct(C.DISCOUNT_RATE)}**\n"
                    f"- License delivery cost: **{C.LICENSE_COGS_PCT_OF_LIST:.0%}** of license list per year\n"
                    "- Freight and duty borne by the vendor (DDP), by country\n"
                    "- FX variance measured against the local price-book rate")
    st.markdown("##### Product catalog")
    cat = pd.DataFrame([{"SKU": s, "Product line": v[0], "Description": v[1], "Hardware list": v[2],
                         "Hardware COGS": v[3], "Hardware margin at list": 1 - v[3] / v[2],
                         "License list / yr": v[4]} for s, v in C.SKUS.items()])
    st.dataframe(cat.style.format({"Hardware list": "${:,.0f}", "Hardware COGS": "${:,.0f}",
                                   "Hardware margin at list": "{:.0%}", "License list / yr": "${:,.0f}"}),
                 hide_index=True, width="stretch")


# --------------------------------------------------------------------------
PAGES = {
    "Overview": (":material/home:", overview_view),
    "Desk queue": (":material/inbox:", desk_view),
    "New request": (":material/edit_note:", rep_view),
    "Leakage analytics": (":material/query_stats:", analytics_view),
    "Pricing policy": (":material/gavel:", policy_view),
}
with st.sidebar:
    st.markdown("### :material/request_quote: Deal Desk")
    page = st.radio("Navigate", list(PAGES), format_func=lambda p: f"{PAGES[p][0]} {p}",
                    label_visibility="collapsed")
    st.divider()
    st.caption("Hardware + subscription pricing governance. Synthetic deal book of 4,000 opportunities.")
PAGES[page][1]()
