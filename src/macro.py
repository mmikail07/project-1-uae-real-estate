"""Pull UAE macro indicators into the macro_indicators table.

Sources:
  - UAE discount rate     -> FRED series INTDSRAEM193N
  - Brent crude oil       -> yfinance BZ=F
  - UAE CPI (annual %)    -> World Bank API (indicator FP.CPI.TOTL.ZG)

Usage:
    python -m src.macro --refresh
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pandas as pd
import requests

from src.config import FRED_API_KEY
from src.db import connect

NOW = datetime.now(timezone.utc).isoformat()


def _upsert(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO macro_indicators (indicator, obs_date, value, source, pulled_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(indicator, obs_date) DO UPDATE SET
                value     = excluded.value,
                source    = excluded.source,
                pulled_at = excluded.pulled_at
            """,
            rows,
        )
    return len(rows)


def pull_fred_uae_rate() -> int:
    """FRED DFF — US Federal Funds Effective Rate, daily.
    Used as a proxy for UAE rates: the dirham is USD-pegged at 3.6725 since 1997,
    so the CBUAE Base Rate moves in near-lockstep with Fed Funds. The UAE-specific
    series INTDSRAEM193N was discontinued, and DFF is the standard analyst proxy.
    """
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"
    if FRED_API_KEY:
        url += f"&api_key={FRED_API_KEY}"
    df = pd.read_csv(url, parse_dates=["observation_date"], na_values=["."])
    df = df.dropna()
    rows = [
        ("us_fed_funds_rate", d.strftime("%Y-%m-%d"), float(v), "FRED", NOW)
        for d, v in zip(df["observation_date"], df["DFF"])
    ]
    return _upsert(rows)


def pull_brent_oil(start: str = "2010-01-01") -> int:
    import yfinance as yf
    ticker = yf.Ticker("BZ=F")
    hist = ticker.history(start=start, auto_adjust=False)
    if hist.empty:
        print("[macro] yfinance returned empty Brent history")
        return 0
    rows = [
        ("brent_oil", idx.strftime("%Y-%m-%d"), float(row["Close"]), "yfinance", NOW)
        for idx, row in hist.iterrows()
    ]
    return _upsert(rows)


def pull_uae_cpi() -> int:
    """World Bank API — UAE CPI annual % change."""
    url = "https://api.worldbank.org/v2/country/ARE/indicator/FP.CPI.TOTL.ZG?format=json&per_page=200"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list) or len(payload) < 2:
        print("[macro] unexpected World Bank response")
        return 0
    rows = [
        ("uae_cpi", f"{rec['date']}-12-31", float(rec["value"]), "world_bank", NOW)
        for rec in payload[1] if rec.get("value") is not None
    ]
    return _upsert(rows)


def refresh_all() -> None:
    for name, fn in [
        ("us_fed_funds_rate", pull_fred_uae_rate),
        ("brent_oil",         pull_brent_oil),
        ("uae_cpi",           pull_uae_cpi),
    ]:
        try:
            n = fn()
            print(f"[macro] {name}: {n:,} rows")
        except Exception as e:
            print(f"[macro] FAILED {name}: {e}")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Macro indicators puller")
    parser.add_argument("--refresh", action="store_true", help="pull all sources")
    args = parser.parse_args()
    if args.refresh:
        refresh_all()
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
