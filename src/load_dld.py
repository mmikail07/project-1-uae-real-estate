"""Ingest DLD-style transaction CSVs into SQLite.

Supports three known layouts via a column-synonym registry:
  1. DLD-native (current dubailand.gov.ae export)
  2. Pulse legacy (older Dubai Pulse extracts)
  3. Common Kaggle mirrors

Idempotent: each (source_file, table) pair is hashed and tracked in load_log;
re-runs are no-ops.

Usage:
    python -m src.load_dld --source data/raw/                    # whole folder
    python -m src.load_dld --source data/raw/transactions_2024.csv
    python -m src.load_dld --source data/raw/ --table rent_contracts
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.config import RAW_DIR
from src.db import connect, init_schema

# -------------------------------------------------------------------
# COLUMN SYNONYM REGISTRY
#   key = canonical column in our schema
#   value = ordered list of accepted source spellings (first match wins)
# -------------------------------------------------------------------

TXN_SYNONYMS: dict[str, list[str]] = {
    "txn_number":       ["transaction_number", "trans_number", "transaction_id", "id"],
    "txn_date":         ["instance_date", "transaction_date", "date_of_transaction", "date", "txn_date"],
    "txn_type":         ["trans_group_en", "transaction_type", "group_en", "trans_group"],
    "procedure_name":   ["procedure_name_en", "procedure_name"],
    "property_type":    ["property_type_en", "property_type"],
    "property_sub_type": ["property_sub_type_en", "property_sub_type"],
    "usage":            ["property_usage_en", "usage_en", "usage"],
    "bedrooms":         ["rooms_en", "no_of_bedrooms", "bedrooms", "rooms"],
    "area_sqft":        ["procedure_area", "area_sqft", "size_sqft", "area"],
    "price_aed":        ["actual_worth", "transaction_size_sq_m_", "amount", "price", "transaction_value"],
    "is_offplan":       ["is_offplan_en", "reg_type_en", "is_offplan", "offplan_or_ready", "offplan"],
    "is_freehold":      ["is_freehold_en", "is_freehold", "freehold"],
    "area_name":        ["area_name_en", "area_name", "area", "neighborhood", "community"],
    "project_name":     ["project_name_en", "project_name", "project"],
    "master_project":   ["master_project_en", "master_project"],
    "building_name":    ["building_name_en", "building_name"],
    "nearest_metro":    ["nearest_metro_en", "nearest_metro"],
    "nearest_mall":     ["nearest_mall_en", "nearest_mall"],
    "nearest_landmark": ["nearest_landmark_en", "nearest_landmark"],
}

RENT_SYNONYMS: dict[str, list[str]] = {
    "contract_number":  ["contract_number", "contract_id"],
    "contract_date":    ["contract_start_date", "registration_date", "date", "contract_date"],
    "annual_rent_aed":  ["annual_amount", "contract_amount", "rent_value", "annual_rent"],
    "property_type":    ["property_type_en", "property_type"],
    "bedrooms":         ["rooms_en", "no_of_bedrooms", "bedrooms"],
    "area_sqft":        ["actual_area", "area_sqft", "area"],
    "area_name":        ["area_name_en", "area_name", "area"],
    "project_name":     ["project_name_en", "project_name"],
}

REQUIRED_TXN_COLS = {"txn_date", "price_aed", "area_name"}
REQUIRED_RENT_COLS = {"contract_date", "annual_rent_aed", "area_name"}


def _norm_header(name: str) -> str:
    """Lowercase + strip + collapse spaces/punct so 'Property Type EN' -> 'property_type_en'."""
    return "".join(c if c.isalnum() else "_" for c in name.strip().lower()).strip("_")


def _resolve_columns(df_cols: list[str], registry: dict[str, list[str]]) -> dict[str, str]:
    """Return mapping {canonical: actual_column_name} for whichever synonyms hit."""
    normalized = {_norm_header(c): c for c in df_cols}
    mapping: dict[str, str] = {}
    for canonical, synonyms in registry.items():
        for syn in synonyms:
            if syn in normalized:
                mapping[canonical] = normalized[syn]
                break
    return mapping


def _detect_table(df_cols: list[str]) -> str:
    """Decide whether a CSV is transactions or rent_contracts by which registry resolves more required fields."""
    txn_hits = len(REQUIRED_TXN_COLS & set(_resolve_columns(df_cols, TXN_SYNONYMS)))
    rent_hits = len(REQUIRED_RENT_COLS & set(_resolve_columns(df_cols, RENT_SYNONYMS)))
    if rent_hits > txn_hits:
        return "rent_contracts"
    return "transactions"


# -------------------------------------------------------------------
# CASTING + DERIVED FIELDS
# -------------------------------------------------------------------

def _to_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", dayfirst=True).dt.strftime("%Y-%m-%d")


def _to_bool_int(s: pd.Series) -> pd.Series:
    """Truthy if value is yes/true/1/freehold OR contains 'off-plan' (handles
    DLD reg_type_en values like 'Off-Plan Properties' vs 'Existing Properties').
    Non-capturing group `(?:...)` avoids a pandas regex-groups UserWarning."""
    norm = s.astype(str).str.strip().str.lower()
    pattern = r"^(?:yes|y|true|t|1|freehold)$|off[\s\-]?plan"
    return norm.str.contains(pattern, regex=True, na=False).astype(int)


def _shape_transactions(df: pd.DataFrame, mapping: dict[str, str], source_file: str) -> pd.DataFrame:
    out = pd.DataFrame()
    for canonical, source_col in mapping.items():
        out[canonical] = df[source_col]

    out["txn_date"] = _to_date(out["txn_date"])
    out["area_sqft"] = pd.to_numeric(out.get("area_sqft"), errors="coerce")
    out["price_aed"] = pd.to_numeric(out.get("price_aed"), errors="coerce")
    out["price_per_sqft"] = (out["price_aed"] / out["area_sqft"]).where(out["area_sqft"] > 0)

    for col in ("is_offplan", "is_freehold"):
        if col in out.columns:
            out[col] = _to_bool_int(out[col])
        else:
            out[col] = 0

    out["source_file"] = source_file
    out["iqr_flag"] = 0

    # drop rows missing the bare minimum
    out = out.dropna(subset=["txn_date", "price_aed"])
    return out


def _shape_rent(df: pd.DataFrame, mapping: dict[str, str], source_file: str) -> pd.DataFrame:
    out = pd.DataFrame()
    for canonical, source_col in mapping.items():
        out[canonical] = df[source_col]
    out["contract_date"] = _to_date(out["contract_date"])
    out["area_sqft"] = pd.to_numeric(out.get("area_sqft"), errors="coerce")
    out["annual_rent_aed"] = pd.to_numeric(out.get("annual_rent_aed"), errors="coerce")
    out["rent_per_sqft"] = (out["annual_rent_aed"] / out["area_sqft"]).where(out["area_sqft"] > 0)
    out["source_file"] = source_file
    out = out.dropna(subset=["contract_date", "annual_rent_aed"])
    return out


# -------------------------------------------------------------------
# AREA/PROJECT DIMENSION POPULATION
# -------------------------------------------------------------------

def _upsert_dim(conn, table: str, name_col: str, names: Iterable[str]) -> dict[str, int]:
    """Insert any unseen names; return {name -> id} for all requested names.
    Defensive against pandas NaN (a float) values from empty CSV cells."""
    clean = sorted({
        str(n).strip()
        for n in names
        if pd.notna(n) and str(n).strip()
    })
    if not clean:
        return {}
    conn.executemany(
        f"INSERT OR IGNORE INTO {table} ({name_col}) VALUES (?)",
        [(n,) for n in clean],
    )
    rows = conn.execute(
        f"SELECT {name_col}, {table[:-1]}_id FROM {table} WHERE {name_col} IN ({','.join('?' * len(clean))})",
        clean,
    ).fetchall()
    return {r[name_col]: r[f"{table[:-1]}_id"] for r in rows}


# -------------------------------------------------------------------
# FILE-LEVEL LOAD
# -------------------------------------------------------------------

def _sha1(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


def _already_loaded(conn, source_file: str, table: str, sha1: str) -> bool:
    row = conn.execute(
        "SELECT sha1 FROM load_log WHERE source_file = ? AND table_name = ?",
        (source_file, table),
    ).fetchone()
    return row is not None and row["sha1"] == sha1


def _record_load(conn, source_file: str, table: str, rows: int, sha1: str) -> None:
    conn.execute(
        """
        INSERT INTO load_log (source_file, table_name, rows_inserted, loaded_at, sha1)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_file, table_name) DO UPDATE SET
            rows_inserted = excluded.rows_inserted,
            loaded_at     = excluded.loaded_at,
            sha1          = excluded.sha1
        """,
        (source_file, table, rows, datetime.now(timezone.utc).isoformat(), sha1),
    )


def load_csv(path: Path, force_table: str | None = None) -> int:
    """Load a single CSV; return rows inserted (0 if already loaded)."""
    sha1 = _sha1(path)
    source_file = path.name

    # peek headers to decide which table
    head = pd.read_csv(path, nrows=0, encoding_errors="ignore")
    df_cols = head.columns.tolist()
    table = force_table or _detect_table(df_cols)
    registry = TXN_SYNONYMS if table == "transactions" else RENT_SYNONYMS
    mapping = _resolve_columns(df_cols, registry)

    required = REQUIRED_TXN_COLS if table == "transactions" else REQUIRED_RENT_COLS
    missing = required - mapping.keys()
    if missing:
        print(f"[load] SKIP {source_file}: detected {table} but missing required fields {missing}")
        print(f"       found columns: {df_cols[:8]}{'...' if len(df_cols) > 8 else ''}")
        return 0

    with connect() as conn:
        if _already_loaded(conn, source_file, table, sha1):
            print(f"[load] SKIP {source_file}: unchanged since last load")
            return 0

        total = 0
        for chunk in pd.read_csv(path, chunksize=50_000, encoding_errors="ignore", low_memory=False):
            shaped = (_shape_transactions if table == "transactions" else _shape_rent)(
                chunk, mapping, source_file
            )
            if shaped.empty:
                continue

            # resolve area_id, project_id
            area_map = _upsert_dim(conn, "areas", "area_name", shaped.get("area_name", pd.Series([], dtype=str)))
            shaped["area_id"] = shaped.get("area_name", pd.Series(index=shaped.index)).map(area_map)

            if "project_name" in shaped.columns:
                proj_map = _upsert_dim(conn, "projects", "project_name", shaped["project_name"])
                shaped["project_id"] = shaped["project_name"].map(proj_map)

            # drop the dimension-name columns; we keep ids only on the fact table
            shaped = shaped.drop(columns=[c for c in ("area_name", "project_name", "master_project") if c in shaped.columns])

            # align with table columns
            table_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            insertable = [c for c in shaped.columns if c in table_cols]
            shaped = shaped[insertable]

            placeholders = ",".join("?" * len(insertable))
            cols_sql = ",".join(insertable)
            conn.executemany(
                f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})",
                shaped.itertuples(index=False, name=None),
            )
            total += len(shaped)

        _record_load(conn, source_file, table, total, sha1)
        print(f"[load] {source_file} -> {table}: {total:,} rows")
        return total


def load_path(source: Path, force_table: str | None = None) -> int:
    """Load a file or every CSV in a directory."""
    if source.is_file():
        return load_csv(source, force_table)
    if source.is_dir():
        csvs = sorted(source.glob("*.csv"))
        if not csvs:
            print(f"[load] no CSVs found in {source}")
            return 0
        return sum(load_csv(p, force_table) for p in csvs)
    raise FileNotFoundError(source)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Ingest DLD CSVs into SQLite")
    parser.add_argument("--source", type=Path, default=RAW_DIR,
                        help="CSV file OR directory of CSVs (default: data/raw/)")
    parser.add_argument("--table", choices=["transactions", "rent_contracts"], default=None,
                        help="force target table (skip auto-detection)")
    parser.add_argument("--init-schema", action="store_true",
                        help="run schema.sql before loading")
    args = parser.parse_args()

    if args.init_schema:
        init_schema()

    total = load_path(args.source, args.table)
    print(f"[load] DONE — {total:,} new rows inserted across all files")


if __name__ == "__main__":
    _cli()
