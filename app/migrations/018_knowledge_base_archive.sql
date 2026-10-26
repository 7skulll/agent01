-- 知识库归档：重复或只读知识库保留历史数据，但默认不出现在选择器，也拒绝新写入。
ALTER TABLE knowledge_bases ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;
