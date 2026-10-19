ALTER TABLE conversations ADD COLUMN purpose TEXT NOT NULL DEFAULT 'chat' CHECK (purpose IN ('chat','evaluation'));
ALTER TABLE runs ADD COLUMN capture_trace BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE evaluations (
 evaluation_id TEXT PRIMARY KEY,
 idempotency_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 status TEXT NOT NULL,
 request JSONB NOT NULL,
 snapshot JSONB NOT NULL,
 results JSONB NOT NULL DEFAULT '[]',
 active_run_id TEXT,
 stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
 error JSONB
);
