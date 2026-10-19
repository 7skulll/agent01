-- 全文索引记录其对应的实际切片版本；旧索引需显式 index 本地重建。
ALTER TABLE lexical_indexes ADD COLUMN chunk_version TEXT;
ALTER TABLE files ADD COLUMN lexical_error TEXT;
