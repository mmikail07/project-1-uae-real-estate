-- Week 2 analytical views over the cleaned + enriched transactions table.
-- Apply via: python -m src.db --views
--
-- All price views share the contract:
--    WHERE txn_type = 'Sales' AND iqr_flag = 0 AND price_per_sqft IS NOT NULL
-- (Mortgages and Gifts excluded; IQR-flagged rows excluded.)
--
-- SQLite has no MEDIAN() aggregate, so medians use the ROW_NUMBER + FILTER trick
-- (SQLite 3.30+). For odd cnt it picks the middle row; for even cnt it averages
-- the two middle rows.

-- ============================================================
-- v_monthly_area_medians  (foundation for most other views)
-- ============================================================
DROP VIEW IF EXISTS v_monthly_area_medians;
CREATE VIEW v_monthly_area_medians AS
WITH ranked AS (
  SELECT
    t.area_id,
    strftime('%Y-%m', t.txn_date)                                AS year_month,
    t.property_type,
    t.price_per_sqft,
    t.price_aed,
    t.area_sqft,
    ROW_NUMBER() OVER (
      PARTITION BY t.area_id, strftime('%Y-%m', t.txn_date), t.property_type
      ORDER BY t.price_per_sqft
    ) AS rn,
    COUNT(*) OVER (
      PARTITION BY t.area_id, strftime('%Y-%m', t.txn_date), t.property_type
    ) AS cnt
  FROM transactions t
  WHERE t.txn_type = 'Sales'
    AND t.iqr_flag = 0
    AND t.price_per_sqft IS NOT NULL
)
SELECT
  area_id,
  year_month,
  property_type,
  cnt AS txn_count,
  AVG(price_per_sqft) FILTER (WHERE rn IN ((cnt+1)/2, (cnt+2)/2)) AS median_price_per_sqft,
  AVG(price_aed)      FILTER (WHERE rn IN ((cnt+1)/2, (cnt+2)/2)) AS median_price_aed,
  AVG(area_sqft)      FILTER (WHERE rn IN ((cnt+1)/2, (cnt+2)/2)) AS median_area_sqft
FROM ranked
GROUP BY area_id, year_month, property_type;

-- ============================================================
-- v_yoy_change_by_area_proptype
--   Builds on v_monthly_area_medians. Joins each row to its counterpart
--   12 months prior to compute YoY % change. NULL where no prior-year row.
-- ============================================================
DROP VIEW IF EXISTS v_yoy_change_by_area_proptype;
CREATE VIEW v_yoy_change_by_area_proptype AS
SELECT
  curr.area_id,
  curr.year_month,
  curr.property_type,
  curr.median_price_per_sqft           AS current_median_pps,
  prev.median_price_per_sqft           AS prior_year_median_pps,
  CASE
    WHEN prev.median_price_per_sqft > 0 THEN
      100.0 * (curr.median_price_per_sqft - prev.median_price_per_sqft)
            / prev.median_price_per_sqft
  END                                  AS yoy_pct_change,
  curr.txn_count                       AS current_txn_count,
  prev.txn_count                       AS prior_year_txn_count
FROM v_monthly_area_medians curr
LEFT JOIN v_monthly_area_medians prev
  ON  prev.area_id       = curr.area_id
  AND prev.property_type = curr.property_type
  AND prev.year_month    = strftime('%Y-%m', date(curr.year_month || '-01', '-12 months'));

-- ============================================================
-- v_offplan_vs_ready_gap
--   Median price/sqft for off-plan vs ready, by area-quarter.
--   The off-plan/ready gap is a market-condition signal — wide when investors
--   are bullish on future supply, narrow when off-plan loses its discount.
-- ============================================================
DROP VIEW IF EXISTS v_offplan_vs_ready_gap;
CREATE VIEW v_offplan_vs_ready_gap AS
WITH quarterly AS (
  SELECT
    t.area_id,
    substr(t.txn_date, 1, 4) || '-Q' || ((CAST(substr(t.txn_date, 6, 2) AS INTEGER) - 1) / 3 + 1)
                                                              AS year_quarter,
    t.is_offplan,
    t.price_per_sqft,
    ROW_NUMBER() OVER (
      PARTITION BY t.area_id,
                   substr(t.txn_date, 1, 4) || '-Q' || ((CAST(substr(t.txn_date, 6, 2) AS INTEGER) - 1) / 3 + 1),
                   t.is_offplan
      ORDER BY t.price_per_sqft
    ) AS rn,
    COUNT(*) OVER (
      PARTITION BY t.area_id,
                   substr(t.txn_date, 1, 4) || '-Q' || ((CAST(substr(t.txn_date, 6, 2) AS INTEGER) - 1) / 3 + 1),
                   t.is_offplan
    ) AS cnt
  FROM transactions t
  WHERE t.txn_type = 'Sales'
    AND t.iqr_flag = 0
    AND t.price_per_sqft IS NOT NULL
    AND t.is_offplan IS NOT NULL
)
SELECT
  area_id,
  year_quarter,
  AVG(CASE WHEN is_offplan = 1 AND rn IN ((cnt+1)/2, (cnt+2)/2)
           THEN price_per_sqft END)                            AS median_pps_offplan,
  AVG(CASE WHEN is_offplan = 0 AND rn IN ((cnt+1)/2, (cnt+2)/2)
           THEN price_per_sqft END)                            AS median_pps_ready,
  SUM(CASE WHEN is_offplan = 1 THEN 1 ELSE 0 END)              AS offplan_txn_count,
  SUM(CASE WHEN is_offplan = 0 THEN 1 ELSE 0 END)              AS ready_txn_count
FROM quarterly
GROUP BY area_id, year_quarter;

-- ============================================================
-- v_rolling_90d_area
--   Rolling 90-day mean price/sqft per area. Useful for smoothing the noisy
--   monthly series in the Tableau line charts. Uses julianday() for the RANGE
--   window because SQLite RANGE clauses require numeric ordering.
--
--   We use AVG (not median) for rolling — correct enough since iqr_flag = 0
--   has already removed the heavy-tail outliers. True rolling median would
--   require per-row window medians, not feasible in plain SQL.
-- ============================================================
DROP VIEW IF EXISTS v_rolling_90d_area;
CREATE VIEW v_rolling_90d_area AS
SELECT
  area_id,
  txn_date,
  AVG(price_per_sqft) OVER (
    PARTITION BY area_id
    ORDER BY julianday(txn_date)
    RANGE BETWEEN 90 PRECEDING AND CURRENT ROW
  ) AS rolling_90d_mean_pps,
  COUNT(*) OVER (
    PARTITION BY area_id
    ORDER BY julianday(txn_date)
    RANGE BETWEEN 90 PRECEDING AND CURRENT ROW
  ) AS rolling_90d_txn_count
FROM transactions
WHERE txn_type = 'Sales'
  AND iqr_flag = 0
  AND price_per_sqft IS NOT NULL;

-- ============================================================
-- v_supply_pipeline_proxy
--   Count of NEW projects (first observed transaction year) per area per year.
--   Proxy for handover/launch volume since projects.handover_year is unpopulated.
-- ============================================================
DROP VIEW IF EXISTS v_supply_pipeline_proxy;
CREATE VIEW v_supply_pipeline_proxy AS
WITH project_first_year AS (
  SELECT
    project_id,
    MIN(CAST(substr(txn_date, 1, 4) AS INTEGER))  AS first_txn_year
  FROM transactions
  WHERE project_id IS NOT NULL AND txn_type = 'Sales'
  GROUP BY project_id
),
project_area AS (
  -- pick the area where the project saw the most transactions (most projects
  -- live in a single area anyway, but a few span multiple — pick the modal one)
  SELECT project_id, area_id
  FROM (
    SELECT
      project_id, area_id,
      ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY COUNT(*) DESC) AS rn
    FROM transactions
    WHERE project_id IS NOT NULL
    GROUP BY project_id, area_id
  )
  WHERE rn = 1
)
SELECT
  pa.area_id,
  pfy.first_txn_year             AS handover_year_proxy,
  COUNT(DISTINCT pfy.project_id) AS new_projects
FROM project_first_year pfy
JOIN project_area pa ON pa.project_id = pfy.project_id
GROUP BY pa.area_id, pfy.first_txn_year;
