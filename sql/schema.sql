-- SQLite schema for Dubai real estate analytics.
-- Run via: python -m src.db --init
-- Tables follow DLD's open-data column conventions but use normalized names.

PRAGMA foreign_keys = ON;

-- ============================================================
-- DIMENSIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS areas (
    area_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    area_name            TEXT    NOT NULL UNIQUE,    -- DLD canonical (system-of-record)
    display_name         TEXT,                       -- colloquial; dashboards use COALESCE(display_name, area_name)
    raw_aliases_json     TEXT,                       -- JSON array of raw spellings
    lat                  REAL,
    lon                  REAL,
    geocode_source       TEXT,                       -- 'nominatim' | 'manual' | NULL
    geocoded_at          TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    project_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name         TEXT    NOT NULL UNIQUE,
    master_project       TEXT,
    developer            TEXT,
    handover_year        INTEGER
);

CREATE TABLE IF NOT EXISTS brokers (
    broker_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_name          TEXT    NOT NULL UNIQUE,
    license_number       TEXT
);

-- ============================================================
-- FACTS
-- ============================================================

CREATE TABLE IF NOT EXISTS transactions (
    txn_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_number           TEXT,                       -- DLD transaction_number
    txn_date             TEXT    NOT NULL,           -- ISO date (YYYY-MM-DD)
    txn_type             TEXT,                       -- Sales/Mortgage/Gift
    procedure_name       TEXT,
    property_type        TEXT,                       -- Unit/Villa/Land/Building
    property_sub_type    TEXT,
    usage                TEXT,                       -- Residential/Commercial/...
    bedrooms             TEXT,                       -- raw DLD: Studio/1 B/R/Office/...
    bedroom_category     TEXT,                       -- Studio/1BR/2BR/3BR/4BR+/Penthouse/Non-residential
    area_sqft            REAL,
    price_aed            REAL,
    price_per_sqft       REAL,                       -- computed at load time
    is_offplan           INTEGER,                    -- 0/1
    is_freehold          INTEGER,                    -- 0/1
    area_id              INTEGER,
    project_id           INTEGER,
    building_name        TEXT,
    nearest_metro        TEXT,
    nearest_mall         TEXT,
    nearest_landmark     TEXT,
    source_file          TEXT NOT NULL,              -- which raw CSV this came from
    iqr_flag             INTEGER DEFAULT 0,          -- 1 if price/sqft is outlier (set in enrich)
    FOREIGN KEY (area_id)    REFERENCES areas(area_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_txn_date          ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_txn_area          ON transactions(area_id);
CREATE INDEX IF NOT EXISTS idx_txn_property_type ON transactions(property_type);
CREATE INDEX IF NOT EXISTS idx_txn_source_file   ON transactions(source_file);
CREATE INDEX IF NOT EXISTS idx_txn_bedroom_cat   ON transactions(bedroom_category);

CREATE TABLE IF NOT EXISTS rent_contracts (
    contract_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_number      TEXT,
    contract_date        TEXT NOT NULL,
    annual_rent_aed      REAL,
    property_type        TEXT,
    bedrooms             TEXT,
    area_sqft            REAL,
    rent_per_sqft        REAL,                       -- computed at load time
    area_id              INTEGER,
    project_id           INTEGER,
    source_file          TEXT NOT NULL,
    FOREIGN KEY (area_id)    REFERENCES areas(area_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_rent_date ON rent_contracts(contract_date);
CREATE INDEX IF NOT EXISTS idx_rent_area ON rent_contracts(area_id);

-- ============================================================
-- MACRO (small, pulled separately)
-- ============================================================

CREATE TABLE IF NOT EXISTS macro_indicators (
    indicator            TEXT    NOT NULL,            -- 'uae_discount_rate' / 'brent_oil' / 'uae_cpi'
    obs_date             TEXT    NOT NULL,            -- ISO date
    value                REAL,
    source               TEXT,                        -- 'FRED' / 'yfinance' / 'world_bank'
    pulled_at            TEXT NOT NULL,
    PRIMARY KEY (indicator, obs_date)
);

-- ============================================================
-- LOADER METADATA
-- ============================================================

CREATE TABLE IF NOT EXISTS load_log (
    log_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file          TEXT NOT NULL,
    table_name           TEXT NOT NULL,
    rows_inserted        INTEGER NOT NULL,
    loaded_at            TEXT NOT NULL,
    sha1                 TEXT                          -- file checksum for idempotency
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_load_log_file_table
    ON load_log(source_file, table_name);
