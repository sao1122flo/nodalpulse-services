-- #88 one-time backfill: populate dockets.title for rows where title IS NULL.
-- Uses the most recently filed filing linked to each docket.
-- Safe to re-run: WHERE d.title IS NULL means already-filled rows are untouched.
-- Run early in the #89 window so CPUC/PJM cards show labels during beta.

UPDATE dockets d
SET title = (
    SELECT f.title
    FROM filings f
    WHERE f.docket_id = d.id
      AND f.title IS NOT NULL
      AND f.title != ''
    ORDER BY f.filed_at DESC
    LIMIT 1
)
WHERE d.title IS NULL;
