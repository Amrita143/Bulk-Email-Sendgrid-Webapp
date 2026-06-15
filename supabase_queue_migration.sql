-- Durable retry queue and dead-letter queue for bulk email campaigns.
-- Run this in Supabase SQL Editor before setting EMAIL_QUEUE_ENABLED=true.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS email_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    email_index INTEGER NOT NULL,
    to_email TEXT NOT NULL DEFAULT '',
    row_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'retry_scheduled', 'sent', 'dead_lettered', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_by TEXT,
    locked_until TIMESTAMPTZ,
    last_status_code INTEGER,
    last_error_type TEXT,
    last_error_message TEXT,
    last_attempt_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, email_index)
);

CREATE TABLE IF NOT EXISTS email_dead_letters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    email_job_id UUID REFERENCES email_jobs(id) ON DELETE SET NULL,
    email_index INTEGER NOT NULL,
    to_email TEXT NOT NULL DEFAULT '',
    row_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_status_code INTEGER,
    error_type TEXT NOT NULL,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    dead_lettered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_jobs_due
    ON email_jobs (status, next_attempt_at, locked_until)
    WHERE status IN ('queued', 'retry_scheduled', 'processing');

CREATE INDEX IF NOT EXISTS idx_email_jobs_campaign
    ON email_jobs (campaign_id, email_index);

CREATE INDEX IF NOT EXISTS idx_email_jobs_status_campaign
    ON email_jobs (campaign_id, status);

CREATE INDEX IF NOT EXISTS idx_email_dead_letters_campaign
    ON email_dead_letters (campaign_id, email_index);

CREATE OR REPLACE FUNCTION touch_email_jobs_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_email_jobs_updated_at ON email_jobs;
CREATE TRIGGER trg_email_jobs_updated_at
BEFORE UPDATE ON email_jobs
FOR EACH ROW
EXECUTE FUNCTION touch_email_jobs_updated_at();

CREATE OR REPLACE FUNCTION claim_due_email_jobs(
    p_worker_id TEXT,
    p_limit INTEGER DEFAULT 10,
    p_lock_seconds INTEGER DEFAULT 300
)
RETURNS SETOF email_jobs
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH due_jobs AS (
        SELECT id
        FROM email_jobs
        WHERE (
            status IN ('queued', 'retry_scheduled')
            OR (status = 'processing' AND locked_until < NOW())
        )
          AND next_attempt_at <= NOW()
          AND (locked_until IS NULL OR locked_until < NOW())
        ORDER BY next_attempt_at ASC, created_at ASC
        LIMIT GREATEST(p_limit, 1)
        FOR UPDATE SKIP LOCKED
    )
    UPDATE email_jobs ej
    SET status = 'processing',
        locked_by = p_worker_id,
        locked_until = NOW() + make_interval(secs => GREATEST(p_lock_seconds, 30)),
        last_attempt_at = NOW()
    FROM due_jobs
    WHERE ej.id = due_jobs.id
    RETURNING ej.*;
END;
$$;

CREATE OR REPLACE FUNCTION refresh_campaign_counts(p_campaign_id TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    v_total INTEGER;
    v_sent INTEGER;
    v_failed INTEGER;
    v_open INTEGER;
    v_status TEXT;
BEGIN
    SELECT
        COUNT(*)::INTEGER,
        COUNT(*) FILTER (WHERE status = 'sent')::INTEGER,
        COUNT(*) FILTER (WHERE status = 'dead_lettered')::INTEGER,
        COUNT(*) FILTER (WHERE status IN ('queued', 'processing', 'retry_scheduled'))::INTEGER
    INTO v_total, v_sent, v_failed, v_open
    FROM email_jobs
    WHERE campaign_id = p_campaign_id;

    IF v_total = 0 THEN
        RETURN;
    END IF;

    IF v_open = 0 THEN
        v_status := 'completed';
    ELSE
        v_status := 'running';
    END IF;

    UPDATE campaigns
    SET sent_count = v_sent,
        failed_count = v_failed,
        total_emails = v_total,
        status = v_status,
        finished_at = CASE
            WHEN v_open = 0 AND finished_at IS NULL THEN NOW()
            ELSE finished_at
        END,
        success_percentage = CASE
            WHEN v_total > 0 THEN ROUND((v_sent::NUMERIC / v_total::NUMERIC) * 100, 2)
            ELSE 0
        END
    WHERE id = p_campaign_id;
END;
$$;

ALTER TABLE email_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_dead_letters ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role full access email_jobs" ON email_jobs;
CREATE POLICY "Service role full access email_jobs" ON email_jobs
    FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access email_dead_letters" ON email_dead_letters;
CREATE POLICY "Service role full access email_dead_letters" ON email_dead_letters
    FOR ALL USING (true) WITH CHECK (true);
