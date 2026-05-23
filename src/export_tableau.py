"""Export Tableau-ready CSVs from the SQLite database.

Run via:
    py -m src.export_tableau --all          # everything
    py -m src.export_tableau --transactions # just one

Produces (in dashboard/):
  - transactions_monthly.csv     primary analytical fact: month x area x property_type
  - macro_daily.csv              daily macro indicators (wide format)
  - supply_yearly.csv            new projects per area per year (Meydan story)
  - area_5y_cagr_vs_offplan.csv  scatter input for Opportunity Finder
  - bedroom_mix_yearly.csv       area x year x bedroom_category counts

All CSVs share a `date` column (or year column) Tableau auto-detects as a date.
display_name is used as the dashboard label; area_name (DLD canonical) is kept
as the audit-trail system-of-record column.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.config import PROJECT_ROOT
from src.db import connect

OUT_DIR = PROJECT_ROOT / "dashboard"


def _report(path: Path, df: pd.DataFrame) -> None:
    size_mb = path.stat().st_size / 1024 / 1024
    rel = path.relative_to(PROJECT_ROOT)
    print(f"[export] wrote {rel}  ({len(df):,} rows, {size_mb:.2f} MB)")


# -------------------------------------------------------------------
# 1. transactions_monthly.csv
# -------------------------------------------------------------------
def export_transactions_monthly() -> None:
    """Monthly per-area-per-property-type analytical facts.
    Reads v_monthly_area_medians and joins area dim for display_name + lat/lon.
    Filter `txn_count >= 10` to keep low-noise bins out of charts."""
    sql = """
    SELECT
        m.year_month || '-01'                       AS date,
        CAST(substr(m.year_month, 1, 4) AS INTEGER) AS year,
        m.area_id,
        a.area_name,
        a.display_name,
        COALESCE(a.display_name, a.area_name)       AS area,
        a.lat,
        a.lon,
        m.property_type,
        m.txn_count,
        m.median_price_per_sqft                     AS median_pps,
        m.median_price_aed                          AS median_price,
        m.median_area_sqft
    FROM v_monthly_area_medians m
    JOIN areas a ON a.area_id = m.area_id
    WHERE m.txn_count >= 10
    ORDER BY m.year_month, m.area_id, m.property_type
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn)
    path = OUT_DIR / "transactions_monthly.csv"
    df.to_csv(path, index=False, float_format="%.2f")
    _report(path, df)


# -------------------------------------------------------------------
# 2. macro_daily.csv
# -------------------------------------------------------------------
def export_macro_daily() -> None:
    """Long->wide pivot of macro_indicators. Forward-fills annual CPI to daily."""
    with connect() as conn:
        long = pd.read_sql(
            "SELECT indicator, obs_date AS date, value FROM macro_indicators",
            conn,
        )
    long["date"] = pd.to_datetime(long["date"])
    wide = long.pivot_table(index="date", columns="indicator", values="value")
    wide = wide.sort_index()
    if "uae_cpi" in wide.columns:
        wide["uae_cpi"] = wide["uae_cpi"].ffill()
    # Clip to the range that overlaps transactions (1995..2023) — keeps file small
    wide = wide.loc["1995-01-01":"2023-12-31"].reset_index()
    path = OUT_DIR / "macro_daily.csv"
    wide.to_csv(path, index=False, float_format="%.4f")
    _report(path, wide)


# -------------------------------------------------------------------
# 3. supply_yearly.csv
# -------------------------------------------------------------------
def export_supply_yearly() -> None:
    """New projects per area per year (first-txn-year proxy for handover).
    Joined with area dim for display + map coords."""
    sql = """
    SELECT
        s.area_id,
        a.area_name,
        a.display_name,
        COALESCE(a.display_name, a.area_name) AS area,
        a.lat,
        a.lon,
        s.handover_year_proxy                 AS year,
        s.new_projects
    FROM v_supply_pipeline_proxy s
    JOIN areas a ON a.area_id = s.area_id
    WHERE s.handover_year_proxy BETWEEN 2010 AND 2023
    ORDER BY s.handover_year_proxy, s.new_projects DESC
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn)
    path = OUT_DIR / "supply_yearly.csv"
    df.to_csv(path, index=False)
    _report(path, df)


# -------------------------------------------------------------------
# 4. area_5y_cagr_vs_offplan.csv  (Opportunity Finder scatter)
# -------------------------------------------------------------------
def export_5y_cagr_vs_offplan() -> None:
    """Per named area: 5-year CAGR of median price/sqft, plus current off-plan gap.

    Data ends Feb 2023, so:
      - 'now'    window = Sep 2022 .. Feb 2023 (last 6 months)
      - '5y ago' window = Sep 2017 .. Feb 2018 (same window 5 years earlier)
      - CAGR     = (now / ago) ^ (1/5) - 1
    Off-plan gap is the 2022 (full-year) average of (offplan - ready) / ready %.
    """
    NOW_LO, NOW_HI = "2022-09", "2023-02"
    AGO_LO, AGO_HI = "2017-09", "2018-02"

    with connect() as conn:
        now_df = pd.read_sql(
            f"""SELECT m.area_id,
                       AVG(m.median_price_per_sqft) AS pps_now,
                       SUM(m.txn_count)             AS recent_txn_count
                FROM v_monthly_area_medians m
                WHERE m.property_type = 'Unit'
                  AND m.year_month BETWEEN '{NOW_LO}' AND '{NOW_HI}'
                GROUP BY m.area_id""",
            conn,
        )
        ago_df = pd.read_sql(
            f"""SELECT m.area_id,
                       AVG(m.median_price_per_sqft) AS pps_ago
                FROM v_monthly_area_medians m
                WHERE m.property_type = 'Unit'
                  AND m.year_month BETWEEN '{AGO_LO}' AND '{AGO_HI}'
                GROUP BY m.area_id""",
            conn,
        )
        gap_df = pd.read_sql(
            """SELECT g.area_id,
                      AVG(100.0 * (g.median_pps_offplan - g.median_pps_ready)
                          / NULLIF(g.median_pps_ready, 0)) AS current_offplan_gap_pct
               FROM v_offplan_vs_ready_gap g
               WHERE g.year_quarter LIKE '2022-Q%'
                 AND g.median_pps_offplan IS NOT NULL
                 AND g.median_pps_ready   IS NOT NULL
                 AND g.offplan_txn_count >= 20
                 AND g.ready_txn_count   >= 20
               GROUP BY g.area_id""",
            conn,
        )
        areas_df = pd.read_sql(
            """SELECT area_id, area_name, display_name,
                      COALESCE(display_name, area_name) AS area, lat, lon
               FROM areas WHERE display_name IS NOT NULL""",
            conn,
        )

    df = (areas_df
          .merge(now_df,  on="area_id", how="inner")
          .merge(ago_df,  on="area_id", how="inner")
          .merge(gap_df,  on="area_id", how="left"))

    df = df[df["pps_ago"] > 0]
    df["cagr_5y_pct"] = 100 * ((df["pps_now"] / df["pps_ago"]) ** (1.0 / 5) - 1)
    df = df[df["recent_txn_count"] >= 30]

    out = (df[["area_id", "area_name", "display_name", "area", "lat", "lon",
               "pps_now", "pps_ago", "cagr_5y_pct",
               "current_offplan_gap_pct", "recent_txn_count"]]
           .sort_values("cagr_5y_pct", ascending=False))
    path = OUT_DIR / "area_5y_cagr_vs_offplan.csv"
    out.to_csv(path, index=False, float_format="%.2f")
    _report(path, out)


# -------------------------------------------------------------------
# 5. bedroom_mix_yearly.csv  (Area Drilldown detail panel)
# -------------------------------------------------------------------
def export_bedroom_mix_yearly() -> None:
    """Area x year x bedroom_category counts. Filter txn_type='Sales' AND iqr_flag=0."""
    sql = """
    SELECT
        t.area_id,
        a.area_name,
        a.display_name,
        COALESCE(a.display_name, a.area_name)       AS area,
        CAST(substr(t.txn_date, 1, 4) AS INTEGER)   AS year,
        t.bedroom_category,
        COUNT(*)                                    AS txn_count
    FROM transactions t
    JOIN areas a ON a.area_id = t.area_id
    WHERE t.txn_type = 'Sales' AND t.iqr_flag = 0
      AND t.bedroom_category IS NOT NULL
      AND t.txn_date BETWEEN '2010-01-01' AND '2023-12-31'
    GROUP BY t.area_id, year, t.bedroom_category
    HAVING COUNT(*) >= 5
    ORDER BY t.area_id, year, t.bedroom_category
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn)
    path = OUT_DIR / "bedroom_mix_yearly.csv"
    df.to_csv(path, index=False)
    _report(path, df)


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------
EXPORTS = {
    "transactions": export_transactions_monthly,
    "macro":        export_macro_daily,
    "supply":       export_supply_yearly,
    "cagr":         export_5y_cagr_vs_offplan,
    "bedrooms":     export_bedroom_mix_yearly,
}


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Export Tableau-ready CSVs to dashboard/")
    parser.add_argument("--all", action="store_true", help="run every export")
    for name in EXPORTS:
        parser.add_argument(f"--{name}", action="store_true", help=f"export {name}")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ran = False
    for name, fn in EXPORTS.items():
        if args.all or getattr(args, name):
            fn()
            ran = True
    if not ran:
        parser.print_help()


if __name__ == "__main__":
    _cli()
