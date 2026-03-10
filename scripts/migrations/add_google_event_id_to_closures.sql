-- Migration: Add google_event_id to aa_office_closures
-- Enables Google Calendar sync for office closures.
-- Safe to re-run: uses IF NOT EXISTS.

ALTER TABLE aa_office_closures
    ADD COLUMN IF NOT EXISTS google_event_id TEXT,
    ADD COLUMN IF NOT EXISTS last_synced TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS uq_aa_office_closures_google_event_id
    ON aa_office_closures (google_event_id)
    WHERE google_event_id IS NOT NULL;

COMMENT ON COLUMN aa_office_closures.google_event_id IS
    'Google Calendar event ID. NULL for manually-created closures.';
COMMENT ON COLUMN aa_office_closures.last_synced IS
    'Timestamp of last Calendar sync that touched this record.';
