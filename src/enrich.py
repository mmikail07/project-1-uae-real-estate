"""Week 2 enrichment: turn the raw transactions table into an analysis-ready layer.

Each subcommand is idempotent and can be run independently:
    python -m src.enrich --bedrooms     # categorize rooms_en into clean buckets
    python -m src.enrich --names        # apply data/external/area_common_names.json
    python -m src.enrich --geocode      # populate areas.lat/lon via Nominatim
    python -m src.enrich --outliers     # flag IQR outliers
    python -m src.enrich --all          # all four, in the right order

`--all` orders correctly: names BEFORE geocode (so the gazetteer sees colloquial
names that hit), outliers LAST (after names so we know which areas exist).
"""
from __future__ import annotations

import argparse
import json

from src.config import PROJECT_ROOT
from src.db import connect

AREA_NAMES_JSON     = PROJECT_ROOT / "data" / "external" / "area_common_names.json"
AREA_OVERRIDES_JSON = PROJECT_ROOT / "data" / "external" / "area_geocode_overrides.json"

# -------------------------------------------------------------------
# BEDROOM CATEGORIZATION
# -------------------------------------------------------------------
# DLD's `rooms_en` is human-readable text. We bucket it into a fixed set of
# clean categories that downstream code/dashboards can filter on without
# parsing strings. Mapping done in a single SQL UPDATE — far faster than
# row-by-row Python on 1M rows.

_BEDROOM_CASE = """
UPDATE transactions
SET bedroom_category = CASE
    WHEN bedrooms IS NULL OR TRIM(bedrooms) = '' THEN NULL
    WHEN UPPER(TRIM(bedrooms)) IN ('STUDIO', 'SINGLE ROOM')             THEN 'Studio'
    WHEN UPPER(TRIM(bedrooms)) = '1 B/R'                                THEN '1BR'
    WHEN UPPER(TRIM(bedrooms)) = '2 B/R'                                THEN '2BR'
    WHEN UPPER(TRIM(bedrooms)) = '3 B/R'                                THEN '3BR'
    WHEN UPPER(TRIM(bedrooms)) IN ('4 B/R', '5 B/R', '6 B/R', '7 B/R',
                                   '8 B/R', '9 B/R', '10 B/R')          THEN '4BR+'
    WHEN UPPER(TRIM(bedrooms)) = 'PENTHOUSE'                            THEN 'Penthouse'
    WHEN UPPER(TRIM(bedrooms)) IN ('OFFICE', 'SHOP', 'STORE', 'GYM',
                                   'WORKSHOP', 'WAREHOUSE')             THEN 'Non-residential'
    ELSE 'Other'
END
"""


def categorize_bedrooms() -> None:
    """Populate transactions.bedroom_category from raw DLD rooms_en text.
    Idempotent — re-runs just re-apply the CASE; pre-existing values get overwritten."""
    with connect() as conn:
        before_null = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE bedroom_category IS NULL"
        ).fetchone()[0]
        conn.execute(_BEDROOM_CASE)
        after_null = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE bedroom_category IS NULL"
        ).fetchone()[0]
        dist = conn.execute(
            """SELECT COALESCE(bedroom_category, '(NULL)') AS cat, COUNT(*) AS n
               FROM transactions GROUP BY 1 ORDER BY 2 DESC"""
        ).fetchall()

    print(f"[enrich] bedroom_category: NULL went {before_null:,} -> {after_null:,}")
    print("[enrich] distribution:")
    for r in dist:
        print(f"    {r['cat']:<20} {r['n']:>10,}")


# -------------------------------------------------------------------
# (other subcommands implemented in subsequent Week 2 days)
# -------------------------------------------------------------------

def apply_area_display_names() -> None:
    """Populate areas.display_name from data/external/area_common_names.json.
    Also appends the DLD canonical to raw_aliases_json as a single-element JSON list
    for downstream auditability."""
    mapping = json.loads(AREA_NAMES_JSON.read_text(encoding="utf-8"))
    # Strip documentation keys (anything prefixed with '_')
    mapping = {k: v for k, v in mapping.items() if not k.startswith("_")}

    with connect() as conn:
        # All DLD canonical names actually present in the areas table
        existing = {r["area_name"] for r in conn.execute("SELECT area_name FROM areas")}
        applicable = {k: v for k, v in mapping.items() if k in existing}
        missing = sorted(set(mapping) - existing)

        # Apply display_name + record DLD canonical in raw_aliases_json
        conn.executemany(
            """UPDATE areas
               SET display_name = ?,
                   raw_aliases_json = json_array(area_name)
               WHERE area_name = ?""",
            [(common, dld) for dld, common in applicable.items()],
        )

        # Coverage stats
        n_areas_named = conn.execute(
            "SELECT COUNT(*) FROM areas WHERE display_name IS NOT NULL"
        ).fetchone()[0]
        total_txn = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        covered_txn = conn.execute(
            """SELECT COUNT(*) FROM transactions t
               JOIN areas a ON a.area_id = t.area_id
               WHERE a.display_name IS NOT NULL"""
        ).fetchone()[0]

    print(f"[enrich] display_name applied to {n_areas_named} areas")
    print(f"[enrich] transaction coverage : {covered_txn:,} / {total_txn:,}  ({100*covered_txn/total_txn:.1f}%)")
    if missing:
        print(f"[enrich] JSON had {len(missing)} entries with no matching DLD area_name:")
        for m in missing:
            print(f"    (not in areas table) {m}")


def _clean_for_geocode(name: str) -> str:
    """Strip parentheticals + take part before '/'. Nominatim chokes on
    'Al Wasl (City Walk area)' but resolves 'Al Wasl' cleanly."""
    import re
    name = re.sub(r"\s*\([^)]*\)", "", name)   # remove "(...)" groups
    name = name.split("/")[0]                   # take left of slash
    return name.strip()


def geocode_areas(min_delay: float = 1.1, only_named: bool = False) -> None:
    """Populate areas.lat/lon via Nominatim. Rate-limited per OSM policy (1 req/sec
    minimum; we use 1.1s). Only geocodes rows where lat IS NULL — re-running just
    fills in any that previously failed.

    Query strategy per area (tries in order, stops on first hit):
      1. cleaned display_name + ', Dubai, UAE'  (colloquial names hit OSM best)
      2. cleaned area_name + ', Dubai, UAE'     (fallback for ones where (1) had decorative parens)
    """
    from datetime import datetime, timezone
    from geopy.geocoders import Nominatim  # type: ignore[import-not-found]
    from geopy.extra.rate_limiter import RateLimiter  # type: ignore[import-not-found]

    from src.config import NOMINATIM_USER_AGENT

    geocoder = Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=10)
    geocode = RateLimiter(geocoder.geocode, min_delay_seconds=min_delay, swallow_exceptions=False)

    with connect() as conn:
        query = """SELECT area_id, area_name, display_name FROM areas WHERE lat IS NULL"""
        if only_named:
            query += " AND display_name IS NOT NULL"
        rows = conn.execute(query).fetchall()

    if not rows:
        print("[enrich] geocode: nothing to do (all areas already have lat/lon)")
        return

    print(f"[enrich] geocoding {len(rows)} area(s) at {min_delay}s/req "
          f"(~{int(len(rows) * min_delay * 1.6)}s worst case with fallbacks)")

    hits: list[tuple] = []
    misses: list[str] = []

    for r in rows:
        candidates = []
        if r["display_name"]:
            candidates.append(_clean_for_geocode(r["display_name"]) + ", Dubai, UAE")
        candidates.append(_clean_for_geocode(r["area_name"]) + ", Dubai, UAE")

        loc = None
        used = ""
        for q in candidates:
            try:
                loc = geocode(q)
            except Exception as e:
                print(f"  ! {r['area_name']}: error {e}")
                loc = None
            if loc:
                used = q
                break

        if loc:
            hits.append((loc.latitude, loc.longitude,
                         datetime.now(timezone.utc).isoformat(), r["area_id"]))
            print(f"  ok {r['area_name']:<38} -> {loc.latitude:.4f},{loc.longitude:.4f}  (via {used[:40]!r})")
        else:
            misses.append(r["area_name"])
            print(f"  -- {r['area_name']:<38} no match")

    with connect() as conn:
        conn.executemany(
            """UPDATE areas
               SET lat = ?, lon = ?, geocode_source = 'nominatim', geocoded_at = ?
               WHERE area_id = ?""",
            hits,
        )

    print(f"[enrich] geocoded {len(hits)} hit / {len(misses)} miss "
          f"({100*len(hits)/(len(hits)+len(misses)):.1f}% success)")
    if misses:
        print(f"[enrich] misses ({len(misses)} — add coords to area_geocode_overrides.json if important):")
        for m in misses[:20]:
            print(f"    {m}")
        if len(misses) > 20:
            print(f"    ... and {len(misses) - 20} more")

    apply_geocode_overrides()


def apply_geocode_overrides() -> None:
    """Apply manual lat/lon entries from area_geocode_overrides.json for areas
    Nominatim can't resolve (typically very new off-plan communities not on OSM)."""
    if not AREA_OVERRIDES_JSON.exists():
        return
    from datetime import datetime, timezone

    overrides = json.loads(AREA_OVERRIDES_JSON.read_text(encoding="utf-8"))
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}
    if not overrides:
        return

    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        existing = {r["area_name"] for r in conn.execute("SELECT area_name FROM areas")}
        applied = []
        for dld_name, payload in overrides.items():
            if dld_name not in existing:
                print(f"[enrich] override skip: '{dld_name}' not in areas table")
                continue
            conn.execute(
                """UPDATE areas SET lat = ?, lon = ?, geocode_source = ?, geocoded_at = ?
                   WHERE area_name = ?""",
                (payload["lat"], payload["lon"], payload.get("source", "manual"), now, dld_name),
            )
            applied.append(dld_name)
    if applied:
        print(f"[enrich] applied {len(applied)} manual override(s): {', '.join(applied)}")


def flag_outliers(min_bin: int = 30, iqr_mult: float = 1.5) -> None:
    """Flag IQR outliers on price_per_sqft for Sales transactions.

    Layered fallback chooses the most-specific bin with ≥ min_bin rows:
        1. (area_id, property_type, year)
        2. (area_id, property_type)            -- when layer 1 is too sparse
        3. (property_type)                     -- when both above are too sparse

    A row is flagged when price_per_sqft falls outside
    [Q1 - iqr_mult*IQR, Q3 + iqr_mult*IQR] in its applicable bin.
    Mortgages/Gifts are not flagged — derived views filter on txn_type='Sales' anyway.
    Idempotent: reruns first reset iqr_flag to 0 for sales rows, then re-apply.
    """
    import pandas as pd

    with connect() as conn:
        df = pd.read_sql(
            """SELECT txn_id, area_id, property_type, price_per_sqft,
                      CAST(substr(txn_date, 1, 4) AS INTEGER) AS year
               FROM transactions
               WHERE txn_type = 'Sales' AND price_per_sqft IS NOT NULL""",
            conn,
        )

    if df.empty:
        print("[enrich] flag_outliers: no Sales rows with price_per_sqft")
        return

    print(f"[enrich] flag_outliers: scoring {len(df):,} Sales rows")

    def _bounds(grp_keys: list[str]) -> pd.DataFrame:
        """Q1, Q3, IQR per group; only groups with ≥ min_bin rows."""
        g = df.groupby(grp_keys)["price_per_sqft"]
        stats = pd.DataFrame({
            "q1": g.quantile(0.25),
            "q3": g.quantile(0.75),
            "n":  g.size(),
        }).reset_index()
        stats = stats[stats["n"] >= min_bin].copy()
        stats["lo"] = stats["q1"] - iqr_mult * (stats["q3"] - stats["q1"])
        stats["hi"] = stats["q3"] + iqr_mult * (stats["q3"] - stats["q1"])
        return stats[grp_keys + ["lo", "hi"]]

    L1 = _bounds(["area_id", "property_type", "year"])
    L2 = _bounds(["area_id", "property_type"])
    L3 = _bounds(["property_type"])

    print(f"[enrich]   layer 1 (area×ptype×year) bins: {len(L1):,}")
    print(f"[enrich]   layer 2 (area×ptype) bins      : {len(L2):,}")
    print(f"[enrich]   layer 3 (ptype) bins           : {len(L3):,}")

    # Merge each layer's bounds, taking the most-specific available
    merged = df.merge(L1, on=["area_id", "property_type", "year"], how="left", suffixes=("", "_1"))
    merged = merged.merge(L2, on=["area_id", "property_type"], how="left", suffixes=("", "_2"))
    merged = merged.merge(L3, on=["property_type"], how="left", suffixes=("", "_3"))
    # First non-null wins (layer 1 > 2 > 3)
    merged["lo_eff"] = merged["lo"].fillna(merged["lo_2"]).fillna(merged["lo_3"])
    merged["hi_eff"] = merged["hi"].fillna(merged["hi_2"]).fillna(merged["hi_3"])

    flagged_mask = (
        (merged["price_per_sqft"] < merged["lo_eff"]) |
        (merged["price_per_sqft"] > merged["hi_eff"])
    )
    flagged_ids = merged.loc[flagged_mask, "txn_id"].tolist()

    print(f"[enrich]   flagged {len(flagged_ids):,} of {len(df):,} Sales rows "
          f"({100*len(flagged_ids)/len(df):.1f}%)")

    with connect() as conn:
        # Reset flags on Sales rows (idempotency); then mark the new set
        conn.execute("UPDATE transactions SET iqr_flag = 0 WHERE txn_type = 'Sales'")
        # SQLite has a parameter limit (~999); chunk the IN clause
        CHUNK = 500
        for i in range(0, len(flagged_ids), CHUNK):
            batch = flagged_ids[i:i + CHUNK]
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f"UPDATE transactions SET iqr_flag = 1 WHERE txn_id IN ({placeholders})",
                batch,
            )

    # Sanity check
    with connect() as conn:
        sample = conn.execute(
            """SELECT a.area_name, t.property_type, t.txn_date, t.price_aed,
                      t.area_sqft, t.price_per_sqft
               FROM transactions t JOIN areas a ON a.area_id = t.area_id
               WHERE t.iqr_flag = 1 AND t.txn_type = 'Sales'
               ORDER BY t.price_per_sqft DESC LIMIT 5"""
        ).fetchall()

    print("[enrich] top 5 flagged (highest price_per_sqft) — should look obviously extreme:")
    for r in sample:
        print(f"    {r['area_name']:<25} {r['property_type']:<10} {r['txn_date']}  "
              f"AED {int(r['price_aed']):>14,}  /  {r['area_sqft']:>10.0f} sqft  "
              f"= AED {r['price_per_sqft']:>10,.0f}/sqft")


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Week 2 enrichment passes")
    parser.add_argument("--bedrooms", action="store_true", help="categorize bedrooms")
    parser.add_argument("--names",    action="store_true", help="apply area display names (Day 2)")
    parser.add_argument("--geocode",  action="store_true", help="geocode areas (Day 3)")
    parser.add_argument("--outliers", action="store_true", help="flag IQR outliers (Day 4)")
    parser.add_argument("--all",      action="store_true", help="run all (in correct order)")
    args = parser.parse_args()

    ran_any = False
    if args.all or args.names:
        apply_area_display_names()
        ran_any = True
    if args.all or args.geocode:
        geocode_areas()
        ran_any = True
    if args.all or args.bedrooms:
        categorize_bedrooms()
        ran_any = True
    if args.all or args.outliers:
        flag_outliers()
        ran_any = True
    if not ran_any:
        parser.print_help()


if __name__ == "__main__":
    _cli()
