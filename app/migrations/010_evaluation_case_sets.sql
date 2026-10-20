CREATE TABLE evaluation_case_sets (
 case_set_id TEXT PRIMARY KEY,
 kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
 name TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version > 0),
 tags JSONB NOT NULL DEFAULT '[]',
 cases JSONB NOT NULL,
 corpus JSONB NOT NULL,
 fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(kb_id,name,version)
);
