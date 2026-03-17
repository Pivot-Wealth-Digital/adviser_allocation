-- Migration: Create aa_calendar_watch_channels table
-- Stores Google Calendar push notification channel state (previously in Firestore).
-- Rollback: DROP TABLE IF EXISTS aa_calendar_watch_channels;

CREATE TABLE IF NOT EXISTS aa_calendar_watch_channels (
    doc_id          TEXT PRIMARY KEY,
    calendar_id     TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    expiration_ms   BIGINT NOT NULL,
    webhook_url     TEXT NOT NULL,
    created_at_utc  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_aa_calendar_watch_calendar_id
    ON aa_calendar_watch_channels (calendar_id);
