-- #19 one-time backfill: populate filings.filer for PUCT rows where filer IS NULL.
-- Source: metadata->>'party' was always populated by the PUCT crawler; the column
-- was left empty because the metadata key was "party" instead of "filer".
-- Safe to re-run: WHERE (filer IS NULL OR filer = '') means already-filled rows are untouched.

UPDATE filings
SET filer = metadata->>'party'
WHERE source_id = (SELECT id FROM sources WHERE slug = 'puct')
  AND (filer IS NULL OR filer = '')
  AND metadata->>'party' IS NOT NULL
  AND metadata->>'party' != '';
