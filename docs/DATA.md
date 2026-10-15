# Synthetic deal book: how it is generated

`python -m dealdesk.generate --seed 42` writes `data/dealbook.duckdb`. Every
distribution below is a named constant in [dealdesk/generate.py](../dealdesk/generate.py)
or [dealdesk/config.py](../dealdesk/config.py). Changing the seed redraws the deals
but keeps the shapes.

## Tables

| Table | Grain | Notes |
|---|---|---|
| `deals` | one row per opportunity | attributes, discounts, and every engine metric (waterfall steps, PV margin) |
| `deal_lines` | one row per deal x SKU | quantity per SKU |
| `benchmarks` | one row per closed deal | leave-one-out peer band (P25/median/P75/P90), band status, margin leakage |
| `decisions` | one row per desk decision | written by the console |

## Volume and timing

- 3,920 closed-won deals across 15 quarters (2023Q1 to 2026Q3), plus 80 pending requests in 2026Q4.
- Quarter weights are Q1 0.85, Q2 0.95, Q3 1.00 and Q4 1.35, with 4% annual growth, to reflect end-of-year buying.

## Who is buying

| Segment | Share of deals | Median hardware list | P(reseller) | Term mix 1 / 3 / 5 / 10 yr |
|---|---|---|---|---|
| Enterprise | 18% | $220K | 45% | 10 / 45 / 38 / 7 |
| Mid-Market | 27% | $70K | 70% | 25 / 50 / 22 / 3 |
| SMB | 30% | $15K | 85% | 50 / 38 / 12 / 0 |
| SLED | 13% | $110K | 75% | 5 / 35 / 40 / 20 |
| Education | 12% | $60K | 80% | 8 / 40 / 35 / 17 |

- Hardware value is lognormal with sigma 0.85, which gives a long right tail.
- Public sector buys longer terms and leans on the channel. SMB buys short terms almost entirely through partners.
- The primary product line is drawn by segment (Education leans to Wireless, Enterprise to Switching and Security). Each deal then gets 0 to 2 additional lines, with Dirichlet shares favouring the primary line.
- Partner tier on reseller deals is Gold 35%, Silver 40%, Registered 25%.

## Reps and geography

- There are 64 reps. Each has a home region (NA 55%, EMEA 25%, APAC 14%, LATAM 6%) and a home segment, which 70% of their deals fall in.
- Each rep carries a discount effect drawn from N(0, 2.5 pts). One rep in ten also gets a further +6 pts, standing in for habitual over-discounters.
- The country is drawn within the rep's region.

## Discount model

The blended negotiated discount, off standard price (that is, after the published term discount), is:

```
base[segment]                           Ent 19%, MM 15%, SMB 10%, SLED 21%, Edu 23%
+ 2.5 pts x ln(size / segment median)   bigger deals get more
+ rep effect
+ 2.5 pts if Q4                         quarter-end push
+ 3.5 pts if reseller                   channel conflict, competitive displacement
+ 0.6 pts per year of term              reps trade margin for term
+ noise                                 sd 6.5 pts reseller, 4.5 pts direct
+ 3 pts if pending                      requests open high; the desk negotiates down
clipped to [0, 55%]
```

The blended figure is then split between hardware and license. On longer terms reps
load the concession onto hardware: the tilt is -1, +2, +5 and +7 pts for 1, 3, 5 and
10 years. The license discount is then solved so the blend is unchanged.

## Cost and market assumptions (config.py)

- **Hardware COGS** runs at 42-48% of list by SKU. **License delivery cost** is 12% of license list per year.
- **Published term discount** is 0%, 10%, 17% and 25% for 1, 3, 5 and 10 years.
- **Partner stack** (distributor + reseller margin; deal reg + volume rebate):
  - Gold: 3% + 12%; 3% + 2%
  - Silver: 4% + 10%; 2% + 1%
  - Registered: 5% + 8%; no rebate
  - Direct deals accrue a 0.5% rebate instead.
- **Landed cost** is freight plus duty on hardware, by country. It ranges from 2% (US) to 21.5% (Brazil).
- **FX:** each currency follows a quarterly geometric random walk from its price-book rate, with quarterly vol equal to half the annual vol (6-15% annual). FX variance is net-to-vendor x (book rate / realized rate - 1).
- **Payment terms** of 30, 60 or 90 days are costed at 9% on the first billing (hardware plus year-one license).
- **Discount rate** for PV is 9%.

## Leakage definition

For each closed deal, the 30 nearest peers come from the same segment and region,
excluding the deal itself. If the deal's discount is above the peer P75,
**leakage = PV margin repriced at P75 - actual PV margin**. Using P75 instead of the
median is deliberately conservative: it counts only the part of the discount that
falls outside the normal range.

## Sanity checks on the default seed

| | |
|---|---|
| Pocket price / list | 61.1% |
| Avg negotiated discount | direct 20.6%, reseller 21.8% |
| Avg PV margin | direct 53.2%, reseller 41.5% |
| Deals above peer P75 | 26% (565 above band, 459 outliers) |
| Pending requests below floor | 25 of 80 |
