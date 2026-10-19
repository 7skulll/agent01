CREATE TABLE conversations (
 thread_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ready','running','failed')),
 checkpoint_run_id TEXT, checkpoint_id TEXT, last_error_code TEXT, request_id TEXT, active_run_id TEXT
);
CREATE TABLE runs (
 run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(thread_id),
 request_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, message TEXT NOT NULL, base_run_id TEXT,
 graph_version TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 pause_requested INTEGER NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0,
 tool_calls INTEGER NOT NULL DEFAULT 0, elapsed_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
 event_seq INTEGER NOT NULL DEFAULT 0, execution_started_at TEXT, answer TEXT,
 tools_used TEXT NOT NULL DEFAULT '[]', error TEXT, knowledge_base_id TEXT, sources TEXT NOT NULL DEFAULT '[]',
 UNIQUE(conversation_id,idempotency_key)
);
CREATE INDEX runs_conversation ON runs(conversation_id,created_at);
CREATE TABLE run_events (
 run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE, seq INTEGER NOT NULL,
 event TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_id,seq)
);
CREATE TABLE files (
 id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 file_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, name TEXT NOT NULL, extension TEXT NOT NULL,
 size BIGINT NOT NULL CHECK(size>0 AND size<=209715200), content_hash TEXT NOT NULL,
 storage_path TEXT NOT NULL UNIQUE, storage_backend TEXT NOT NULL,
 index_status TEXT NOT NULL CHECK(index_status IN ('not_indexed','recorded','parsing','indexing','indexed','failed')),
 index_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, chunk_version TEXT, UNIQUE(kb_id,idempotency_key)
);
CREATE INDEX files_kb_name ON files(kb_id,name);
CREATE INDEX files_content_hash ON files(content_hash);
CREATE TABLE chunks (
 chunk_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
 chunk_index INTEGER NOT NULL, content TEXT NOT NULL, tokens INTEGER NOT NULL, heading TEXT,
 heading_level INTEGER, kind TEXT NOT NULL, page INTEGER, source_order INTEGER, created_at TEXT NOT NULL,
 UNIQUE(file_id,chunk_index)
);
CREATE INDEX chunks_file ON chunks(file_id);
CREATE TABLE chunk_vectors (
 chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
 file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE, model TEXT NOT NULL,
 dimension INTEGER NOT NULL, index_version TEXT NOT NULL, input_hash TEXT NOT NULL,
 vector public.vector NOT NULL, created_at TEXT NOT NULL,
 CHECK(public.vector_dims(vector)=dimension)
);
CREATE INDEX vectors_file ON chunk_vectors(file_id);
CREATE TABLE lexical_indexes (
 file_id TEXT PRIMARY KEY REFERENCES files(file_id) ON DELETE CASCADE, version TEXT NOT NULL
);
CREATE TABLE chunk_terms (
 chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
 file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE, kb_id TEXT NOT NULL,
 terms TEXT NOT NULL, tokens TEXT[] NOT NULL, token_count INTEGER NOT NULL
);
CREATE INDEX terms_file ON chunk_terms(file_id);
CREATE TABLE lexical_postings (
 chunk_id TEXT NOT NULL REFERENCES chunk_terms(chunk_id) ON DELETE CASCADE,
 term TEXT NOT NULL, positions INTEGER[] NOT NULL, PRIMARY KEY(term,chunk_id)
);
CREATE INDEX postings_chunk ON lexical_postings(chunk_id);
