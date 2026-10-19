-- 对齐实际查询：唯一索引已覆盖 chunks.file_id 和 runs.conversation_id 的前缀访问。
-- 文件列表/待处理查询按 kb_id、created_at、id 排序，不按 name/content_hash 检索。
DROP INDEX chunks_file;
DROP INDEX runs_conversation;
DROP INDEX files_content_hash;
DROP INDEX files_kb_name;
CREATE INDEX files_kb_created ON files(kb_id,created_at,id);
