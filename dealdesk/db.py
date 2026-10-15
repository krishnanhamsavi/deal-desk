"""DuckDB storage, deal reconstruction, and book-level benchmarking."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from . import config as C
from .economics import Deal, margin

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "dealbook.duckdb"

# Peer distance: hard match on segment (and region when enough peers exist),
# soft match on size, hardware/license mix, primary line, country and channel.
# One unit of distance ~ "half a doubling in size" or "15 pts of hw share".
PEER_DISTANCE_SQL = """
      abs(ln(p.list_price) - ln({size})) / 0.5
    + abs(p.hw_share - {hw_share}) / 0.15
    + CASE WHEN p.primary_line <> {line} THEN 1.0 ELSE 0 END
    + CASE WHEN p.geo <> {geo} THEN 0.5 ELSE 0 END
    + CASE WHEN p.channel <> {channel} THEN 0.5 ELSE 0 END
"""


def connect(path: str | Path = DEFAULT_DB, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path), read_only=read_only)


def write_book(deals: pd.DataFrame, lines: pd.DataFrame, path: str | Path = DEFAULT_DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if Path(path).exists():
        Path(path).unlink()
    con = connect(path)
    con.register("deals_df", deals)
    con.register("lines_df", lines)
    con.execute("CREATE TABLE deals AS SELECT * FROM deals_df")
    con.execute("CREATE TABLE deal_lines AS SELECT * FROM lines_df")
    con.execute("""CREATE TABLE decisions (
        deal_id VARCHAR, decided_at TIMESTAMP, decision VARCHAR,
        approved_discount DOUBLE, counter_path VARCHAR, note VARCHAR)""")
    build_benchmarks(con)
    con.close()


# --------------------------------------------------------------------------
# deal reconstruction
# --------------------------------------------------------------------------
def load_deal(con, deal_id: str) -> Deal:
    r = con.execute("SELECT * FROM deals WHERE deal_id = ?", [deal_id]).df().iloc[0]
    ln = con.execute("SELECT sku, qty FROM deal_lines WHERE deal_id = ? ORDER BY sku", [deal_id]).fetchall()
    return Deal(segment=r.segment, geo=r.geo, channel=r.channel,
                partner_tier=None if pd.isna(r.partner_tier) else r.partner_tier,
                term_years=int(r.term_years), lines=tuple((s, int(q)) for s, q in ln),
                hw_discount=float(r.hw_discount), lic_discount=float(r.lic_discount),
                payment_days=int(r.payment_days), fx_realized=float(r.fx_realized),
                deal_id=r.deal_id, rep=r.rep, close_quarter=r.close_quarter, account=r.account)


def _q(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


# --------------------------------------------------------------------------
# peers
# --------------------------------------------------------------------------
def find_peers(con, d: Deal, s: dict, k: int = C.PEER_K, exclude: str | None = None) -> tuple[pd.DataFrame, str]:
    """k nearest closed-won deals. Returns (peers, match description)."""
    dist = PEER_DISTANCE_SQL.format(size=s["list_price"], hw_share=s["hw_share"],
                                    line=_q(s["primary_line"]), geo=_q(d.geo), channel=_q(d.channel))
    region = C.GEOS[d.geo][0]
    base = f"""
        SELECT p.deal_id, p.account, p.rep, p.segment, p.geo, p.region, p.channel, p.partner_tier,
               p.term_years, p.close_quarter, p.list_price, p.hw_share, p.primary_line,
               p.negotiated_discount, p.margin_pv_pct, p.pocket_pct_of_list, {dist} AS distance
        FROM deals p
        WHERE p.status = 'Closed Won' AND p.segment = {_q(d.segment)}
          AND p.deal_id <> {_q(exclude or d.deal_id)}
          {{region_filter}}
        ORDER BY distance, p.deal_id LIMIT {k}"""
    peers = con.execute(base.format(region_filter=f"AND p.region = {_q(region)}")).df()
    match = f"{d.segment} / {region}"
    if len(peers) < C.PEER_MIN:
        peers = con.execute(base.format(region_filter="")).df()
        match = f"{d.segment} / all regions (fewer than {C.PEER_MIN} in {region})"
    return peers, match


def build_benchmarks(con):
    """Leave-one-out peer band for every closed deal, in one DuckDB pass, then
    margin leakage vs. the top of the band."""
    dist = PEER_DISTANCE_SQL.format(size="d.list_price", hw_share="d.hw_share", line="d.primary_line",
                                    geo="d.geo", channel="d.channel")
    lo, hi = C.PEER_BAND
    bench = con.execute(f"""
        WITH pairs AS (
            SELECT d.deal_id, p.negotiated_discount AS peer_disc,
                   row_number() OVER (PARTITION BY d.deal_id ORDER BY {dist}, p.deal_id) AS rk
            FROM deals d JOIN deals p
              ON p.segment = d.segment AND p.region = d.region
             AND p.deal_id <> d.deal_id AND p.status = 'Closed Won'
            WHERE d.status = 'Closed Won'
        )
        SELECT deal_id, count(*) AS n_peers,
               quantile_cont(peer_disc, {lo}) AS peer_p25,
               quantile_cont(peer_disc, 0.5) AS peer_median,
               quantile_cont(peer_disc, {hi}) AS peer_p75,
               quantile_cont(peer_disc, {C.PEER_OUTLIER_Q}) AS peer_p90
        FROM pairs WHERE rk <= {C.PEER_K}
        GROUP BY deal_id
    """).df()

    deals = con.execute("SELECT deal_id, negotiated_discount, margin_pv FROM deals WHERE status='Closed Won'").df()
    bench = bench.merge(deals, on="deal_id")
    leak = []
    for r in bench.itertuples():
        if r.negotiated_discount > r.peer_p75:
            d = load_deal(con, r.deal_id)
            leak.append(max(0.0, margin(d.with_discount(r.peer_p75)).margin_pv - r.margin_pv))
        else:
            leak.append(0.0)
    bench["margin_leakage"] = leak
    bench["band_status"] = [band_status(x, a, b, c) for x, a, b, c in
                            zip(bench.negotiated_discount, bench.peer_p25, bench.peer_p75, bench.peer_p90)]
    bench = bench.drop(columns=["negotiated_discount", "margin_pv"])
    con.register("bench_df", bench)
    con.execute("CREATE OR REPLACE TABLE benchmarks AS SELECT * FROM bench_df")


def band_status(x, p25, p75, p90) -> str:
    if x > p90:
        return "Outlier"
    if x > p75:
        return "Above band"
    if x < p25:
        return "Below band"
    return "In band"


# --------------------------------------------------------------------------
# book analytics
# --------------------------------------------------------------------------
DISCOUNT_BUCKETS = """
    CASE WHEN negotiated_discount < 0.10 THEN '0-10%'
         WHEN negotiated_discount < 0.20 THEN '10-20%'
         WHEN negotiated_discount < 0.30 THEN '20-30%'
         ELSE '30%+' END"""


def leakage_by(con, dims: list[str]) -> pd.DataFrame:
    cols = ", ".join(dims)
    return con.execute(f"""
        SELECT {cols}, count(*) AS deals,
               sum(margin_leakage) AS margin_leakage,
               sum(list_price) AS list_price,
               sum(pocket_price) AS pocket_price,
               avg(negotiated_discount) AS avg_discount,
               sum(CASE WHEN band_status IN ('Above band','Outlier') THEN 1 ELSE 0 END)::INT AS above_band
        FROM (SELECT d.*, {DISCOUNT_BUCKETS} AS discount_bucket, b.margin_leakage, b.band_status
              FROM deals d JOIN benchmarks b USING (deal_id))
        GROUP BY {cols} ORDER BY {cols}""").df()


def headline(con) -> dict:
    r = con.execute("""
        SELECT sum(b.margin_leakage) AS total_leakage,
               sum(CASE WHEN d.channel='Reseller' AND d.negotiated_discount >= 0.30 THEN b.margin_leakage END)
                   AS reseller_30,
               sum(CASE WHEN d.channel='Reseller' AND d.negotiated_discount >= 0.30 THEN 1 ELSE 0 END) AS reseller_30_n,
               count(*) AS n, sum(d.list_price) AS list, sum(d.pocket_price) AS pocket,
               sum(d.margin_pv) AS margin_pv
        FROM deals d JOIN benchmarks b USING (deal_id)""").fetchone()
    total, r30, r30n, n, lst, pocket, mpv = r
    return {"total_leakage": total, "reseller_30_leakage": r30 or 0.0,
            "reseller_30_share": (r30 or 0.0) / total if total else 0.0,
            "reseller_30_deals": r30n, "reseller_30_deal_share": r30n / n,
            "n_deals": n, "list": lst, "pocket": pocket, "pocket_pct": pocket / lst, "margin_pv": mpv}


def portfolio_waterfall(con, where: str = "TRUE") -> pd.DataFrame:
    cols = ["wf_term_discount", "wf_neg_hardware", "wf_neg_license", "wf_partner_margin",
            "wf_rebates_deal_reg", "wf_payment_terms", "wf_freight_duty", "wf_fx_variance"]
    sums = ", ".join(f"sum({c}) AS {c}" for c in cols)
    return con.execute(f"SELECT sum(list_price) AS list_price, {sums}, sum(invoice_price) AS invoice_price, "
                       "sum(pocket_price) AS pocket_price FROM deals "
                       f"WHERE status='Closed Won' AND {where}").df()


def insert_request(con, row: dict, lines: list):
    cols = con.execute("SELECT * FROM deals LIMIT 0").df().columns
    df = pd.DataFrame([{c: row.get(c) for c in cols}])
    con.register("new_row", df)
    con.execute("INSERT INTO deals SELECT * FROM new_row")
    con.unregister("new_row")
    for sku, qty in lines:
        con.execute("INSERT INTO deal_lines VALUES (?, ?, ?, ?)", [row["deal_id"], sku, C.SKUS[sku][0], qty])


def ensure_book(path: str | Path = DEFAULT_DB):
    if not Path(path).exists():
        from .generate import generate
        deals, lines = generate()
        write_book(deals, lines, path)
