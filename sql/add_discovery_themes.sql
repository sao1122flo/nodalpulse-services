-- B3 Discovery themes (FERC-only v1).
-- Shared curated theme taxonomy + per-filing multi-label matches.
-- Classification is GLOBAL (one Haiku pass per discovery_feed row vs the taxonomy);
-- per-user "untracked" filtering and theme subscription happen at read time (web).
-- uuid_generate_v4 / pg_trgm already enabled by services_schema.sql.

-- ── themes: curated taxonomy (we edit this, not users) ───────────────────────
CREATE TABLE IF NOT EXISTS themes (
  id          UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
  key         TEXT        NOT NULL UNIQUE,        -- stable slug; web references this
  label       TEXT        NOT NULL,
  definition  TEXT        NOT NULL,               -- feeds the Haiku classification prompt
  sort_order  INT         NOT NULL DEFAULT 0,
  active      BOOLEAN     NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the 6 ICP themes (idempotent). Definitions are written for the classifier:
-- specific enough that Haiku only matches clearly on-topic filings (G3).
INSERT INTO themes (key, label, definition, sort_order) VALUES
  ('cost_allocation', 'Cost Allocation',
   'Allocation or recovery of transmission or generation costs among customers, zones, or regions — cost-responsibility disputes, allocation methodology, or who pays for a project or service.', 1),
  ('roe', 'Return on Equity (ROE)',
   'Authorized return on equity or rate of return for a utility or transmission owner, including ROE complaints under FPA section 206, base-ROE determinations, and risk-premium or incentive ROE adders.', 2),
  ('bess', 'Battery Storage (BESS)',
   'Battery energy storage systems specifically — storage interconnection, market participation models, capacity accreditation for storage, or compensation rules for storage resources.', 3),
  ('rpm', 'Capacity Market / RPM',
   'Capacity-market auctions and rules, especially the PJM Reliability Pricing Model (RPM) — auction parameters, results, capacity accreditation, or capacity performance.', 4),
  ('demand_response', 'Demand Response',
   'Demand response and demand-side or distributed energy resource participation in wholesale markets — eligibility, measurement and verification, or compensation.', 5),
  ('transmission_cost_recovery', 'Transmission Cost Recovery',
   'Transmission formula-rate filings, transmission cost recovery, or transmission incentives such as CWIP, abandoned-plant recovery, or RTO-participation adders.', 6)
ON CONFLICT (key) DO NOTHING;

-- ── discovery_matches: theme tags on a discovery_feed row (multi-label) ───────
CREATE TABLE IF NOT EXISTS discovery_matches (
  id               UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
  discovery_id     UUID        NOT NULL REFERENCES discovery_feed(id) ON DELETE CASCADE,
  theme_id         UUID        NOT NULL REFERENCES themes(id)         ON DELETE CASCADE,
  evidence_snippet TEXT,                          -- verbatim substring of description, or NULL (G1)
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (discovery_id, theme_id)
);
CREATE INDEX IF NOT EXISTS discovery_matches_theme_idx     ON discovery_matches (theme_id);
CREATE INDEX IF NOT EXISTS discovery_matches_discovery_idx ON discovery_matches (discovery_id);

-- ── classification marker on discovery_feed ──────────────────────────────────
-- Set after the Haiku pass runs for a row (even with 0 matches) so 0-match rows
-- are never re-classified. Classify WHERE themed_at IS NULL.
ALTER TABLE discovery_feed ADD COLUMN IF NOT EXISTS themed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS discovery_feed_themed_at_idx ON discovery_feed (themed_at);
