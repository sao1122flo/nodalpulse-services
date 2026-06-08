-- #79: Add CPUC as a source for the CpucAdapter.
-- Safe to run multiple times (ON CONFLICT DO NOTHING).

INSERT INTO sources (slug, label, base_url)
VALUES ('cpuc', 'CPUC Document Search', 'https://docs.cpuc.ca.gov')
ON CONFLICT (slug) DO NOTHING;
