-- D1 schema for Color i5 reading uploads.
-- Apply with: npx wrangler d1 execute i5-readings --remote --file schema.sql

CREATE TABLE IF NOT EXISTS readings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  uploaded_at TEXT    NOT NULL DEFAULT (datetime('now')),
  uploaded_by TEXT    NOT NULL DEFAULT 'admin',  -- api_keys.owner of the uploader
  device      TEXT    NOT NULL,           -- e.g. "color-i5" or an instrument serial
  ts          TEXT    NOT NULL,           -- measurement timestamp (ISO-8601, from the GUI)
  label       TEXT    NOT NULL DEFAULT '',
  mode        TEXT    NOT NULL,           -- sci | sce
  L REAL, a REAL, b REAL, C REAL, h REAL,
  agtron      REAL,                       -- provisional L*-derived value
  roast_class TEXT,
  roast_shade TEXT,
  de76        REAL,                       -- distance to nearest SCA curve point
  crc_ok      INTEGER,
  datasum_ok  INTEGER,
  flashes     TEXT,
  status      TEXT,                       -- raw instrument status word
  spectrum    TEXT    NOT NULL            -- JSON array of 40 %R values, 360-750 nm @ 10 nm
);

-- One row per physical reading: re-uploads are no-ops. The spectrum is the
-- fingerprint — 40 floats of measurement noise make every real flash unique,
-- while replayed/recalled copies of the same reading match exactly.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reading ON readings (device, label, mode, spectrum);
CREATE INDEX IF NOT EXISTS ix_readings_uploader ON readings (uploaded_by, uploaded_at);

-- Per-user API keys ("license keys"). The admin key is the API_TOKEN Worker
-- secret, NOT a row here; it manages this table via /api/keys. Keys are stored
-- only as SHA-256 hashes — the plaintext is shown once at creation.
CREATE TABLE IF NOT EXISTS api_keys (
  key_hash    TEXT    PRIMARY KEY,        -- hex SHA-256 of the key
  owner       TEXT    NOT NULL UNIQUE,    -- who you issued it to ("acme-roastery", email, ...)
  can_write   INTEGER NOT NULL DEFAULT 1, -- 0 = read-only key
  revoked     INTEGER NOT NULL DEFAULT 0,
  daily_quota INTEGER NOT NULL DEFAULT 5000,  -- max readings inserted per UTC day
  note        TEXT    NOT NULL DEFAULT '',
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
