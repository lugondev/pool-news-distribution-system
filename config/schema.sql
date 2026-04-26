-- ─────────────────────────────────────────────────────────────────────────
-- Postgres schema for news-aggregator config (Layer-2 backend)
-- ─────────────────────────────────────────────────────────────────────────
-- Applied by `python config/sync.py init`.
-- Idempotent: safe to re-run.
--
-- Design choices:
--   * Each YAML file maps to ONE table (sources, social_agents, sim_personas, settings).
--   * "Known" fields get strict columns for SQL queryability.
--   * "Unknown / nested / variable" fields go into JSONB so YAML schema changes
--     don't require DB migrations.
--   * `created_at` is set on INSERT only (not UPDATE) → preserves row order
--     across updates. `updated_at` reflects last write.
--   * The legacy `config_files` table from the text-blob design is left
--     untouched — drop it manually with `DROP TABLE config_files` once you've
--     verified migration to the relational schema.
-- ─────────────────────────────────────────────────────────────────────────

-- ── 1. sources ──────────────────────────────────────────────────────────
-- 8 known top-level keys observed across 370 sources.
-- `display_order` preserves the YAML array index so round-trip dump matches
-- the visual order operators see in the UI / source file.
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'rss',
    lang          TEXT,
    country       TEXT,
    category      TEXT,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    extra         JSONB NOT NULL DEFAULT '{}'::jsonb,
    display_order INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sources_category_idx ON sources(category);
CREATE INDEX IF NOT EXISTS sources_enabled_idx  ON sources(enabled);
CREATE INDEX IF NOT EXISTS sources_order_idx    ON sources(display_order);

-- ── 2. social_agents ────────────────────────────────────────────────────
-- 6 known keys: id, name, enabled, persona, platforms, source_filter.
-- Last 3 are deeply nested → JSONB. display_order preserves array order.
CREATE TABLE IF NOT EXISTS social_agents (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    persona         JSONB,
    platforms       JSONB,
    source_filter   JSONB,
    display_order   INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 3. sim_personas ─────────────────────────────────────────────────────
-- Source structure: {author_types: {activist: {...}, citizen: {...}}, netizen_types: {...}}
-- → composite PK (type, name) maps cleanly without flattening to surrogate IDs.
CREATE TABLE IF NOT EXISTS sim_personas (
    type        TEXT NOT NULL,    -- 'author_types' | 'netizen_types'
    name        TEXT NOT NULL,    -- 'activist' | 'fact_checker' | etc.
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (type, name)
);

-- ── 4. settings ─────────────────────────────────────────────────────────
-- 21 top-level sections (app, crawler, ai, debate, channels, ...).
-- Sections vary wildly in shape (scalars, dicts, lists) → store each as JSONB.
-- Query example: SELECT data->>'fetch_interval_minutes' FROM settings WHERE section='crawler';
CREATE TABLE IF NOT EXISTS settings (
    section     TEXT PRIMARY KEY,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
