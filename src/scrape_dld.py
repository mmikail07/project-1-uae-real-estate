"""Automate DLD Open Data CSV downloads via Playwright.

The dubailand.gov.ae transactions page is a JS-rendered SPA — the "Download as
CSV" button has no static URL. This script drives a headless browser to set the
date range and click download, saving each year's CSV to data/raw/.

Usage:
    python -m playwright install chromium      # one-time
    python -m src.scrape_dld --years 2010-2024 --dataset transactions
    python -m src.scrape_dld --years 2024 --dataset rents

NOTE: This is best-effort. DLD changes the page periodically; if selectors break,
fall back to manual download (see README "Path A").
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from src.config import DLD_BASE_URL, RAW_DIR

DATASETS = {
    "transactions": "Transactions",
    "rents":        "Rents",
    "brokers":      "Brokers",
    "projects":     "Projects",
}


def parse_year_range(spec: str) -> list[int]:
    """'2010-2024' -> [2010, 2011, ..., 2024]; '2024' -> [2024]."""
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(y) for y in re.split(r"[,\s]+", spec) if y.strip()]


def scrape(years: list[int], dataset: str, out_dir: Path = RAW_DIR, headless: bool = True) -> list[Path]:
    """Drive Chromium to download `dataset` CSV for each year in `years`."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SystemExit(
            "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
        ) from e

    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    label = DATASETS.get(dataset, dataset.title())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.goto(DLD_BASE_URL, wait_until="domcontentloaded", timeout=60_000)

        for year in years:
            target = out_dir / f"dld_{dataset}_{year}.csv"
            if target.exists():
                print(f"[scrape] SKIP {target.name} (already exists)")
                downloaded.append(target)
                continue

            try:
                # Tab selection — labels are visible text on the page.
                page.get_by_text(label, exact=True).first.click(timeout=10_000)

                # Date fields are DD-MM-YYYY per DLD page validation.
                page.locator("input[placeholder*='From' i], input[name*='from' i]").first.fill(f"01-01-{year}")
                page.locator("input[placeholder*='To' i], input[name*='to' i]").first.fill(f"31-12-{year}")

                page.get_by_role("button", name=re.compile("search", re.I)).first.click()
                page.wait_for_load_state("networkidle", timeout=60_000)

                with page.expect_download(timeout=120_000) as dl_info:
                    page.get_by_role("button", name=re.compile("download.*csv", re.I)).first.click()
                dl = dl_info.value
                dl.save_as(target)
                print(f"[scrape] saved {target.name}")
                downloaded.append(target)
            except Exception as e:
                print(f"[scrape] FAILED for {year}: {e}")
                print("        Fall back to manual download (README path A).")

        browser.close()

    return downloaded


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Automated DLD CSV downloader")
    parser.add_argument("--years", default="2024", help="e.g. '2024' or '2010-2024' or '2020,2022,2024'")
    parser.add_argument("--dataset", choices=list(DATASETS), default="transactions")
    parser.add_argument("--headed", action="store_true", help="show browser (debug)")
    args = parser.parse_args()
    files = scrape(parse_year_range(args.years), args.dataset, headless=not args.headed)
    print(f"[scrape] {len(files)} file(s) ready in {RAW_DIR}")


if __name__ == "__main__":
    _cli()
