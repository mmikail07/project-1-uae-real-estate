"""Project-wide paths and constants. Resolved relative to this file so the
package works whether invoked from repo root, a notebook, or a script."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # .env support is optional — env vars work without it

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"

SQL_DIR = PROJECT_ROOT / "sql"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

DB_PATH = Path(os.getenv("DLD_DB_PATH") or (PROJECT_ROOT / "real_estate.db"))

NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "uae-real-estate-portfolio")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

DLD_BASE_URL = "https://dubailand.gov.ae/en/open-data/real-estate-data/"
