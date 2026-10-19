CREATE TABLE knowledge_bases (
 kb_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
INSERT INTO knowledge_bases VALUES ('default', '默认知识库', CURRENT_TIMESTAMP::text);
INSERT INTO knowledge_bases (kb_id, name, created_at)
 SELECT DISTINCT kb_id, kb_id, CURRENT_TIMESTAMP::text FROM files
 ON CONFLICT (kb_id) DO NOTHING;
CREATE UNIQUE INDEX knowledge_bases_name ON knowledge_bases (lower(name));
ALTER TABLE files ADD CONSTRAINT files_knowledge_base_fk
 FOREIGN KEY (kb_id) REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE;
CREATE TABLE file_cleanup_queue (
 storage_path TEXT PRIMARY KEY, created_at TEXT NOT NULL
);
