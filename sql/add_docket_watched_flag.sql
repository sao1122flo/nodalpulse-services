-- Add watched flag to dockets table.
-- watched=true: actively monitored in daily crawl-pjm / crawl-ferc.
-- watched=false (default): referenced by a multi-docket filing caption but not
--   a curated seed — created for filing_dockets threading, not for polling.
-- get_pjm_ferc_docket_set() filters watched=true to keep the daily watch set curated.

ALTER TABLE dockets ADD COLUMN IF NOT EXISTS watched boolean NOT NULL DEFAULT false;

-- Seed the 8 original PJM-FERC hot dockets as watched=true.
UPDATE dockets SET watched = true
WHERE external_id IN (
    'ER25-1357',  -- PJM RPM collar extension
    'EL25-49',    -- PJM data center co-location
    'EL25-46',    -- PJM interconnection complaint
    'ER24-2236',  -- RTEP cost allocation
    'ER24-2238',  -- RTEP cost allocation
    'EL24-119',   -- PJM complaint
    'ER26-1556',  -- PJM collar extension tariff filing
    'ER26-455'    -- PJM collar extension docket
) AND jurisdiction = 'PJM-FERC';

-- CAISO-FERC dockets: watched=true for all (small, curated set).
UPDATE dockets SET watched = true WHERE jurisdiction = 'CAISO-FERC';

-- FERC/PUCT/ERCOT dockets: watched=true for all (small sets).
UPDATE dockets SET watched = true WHERE jurisdiction IN ('FERC', 'PUCT', 'ERCOT');

-- Report
SELECT jurisdiction, watched, COUNT(*) FROM dockets GROUP BY jurisdiction, watched ORDER BY 1, 2;
