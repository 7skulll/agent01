ALTER TABLE conversations ADD COLUMN title TEXT CHECK (length(title) BETWEEN 1 AND 100);
ALTER TABLE runs ADD COLUMN file_ids TEXT;
CREATE TABLE checkpoint_cleanup_queue (
 thread_id TEXT PRIMARY KEY, created_at TEXT NOT NULL
);
