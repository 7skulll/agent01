-- HNSW 近似检索已下线，向量检索固定使用精确余弦。
-- 运行时曾按模型版本创建的 vectors_hnsw_* 索引不再被查询使用，随迁移移除。
DO $$
DECLARE target TEXT;
BEGIN
  FOR target IN
    SELECT i.indexrelid::regclass::text
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_am am ON am.oid = c.relam
    WHERE i.indrelid = 'chunk_vectors'::regclass AND am.amname = 'hnsw'
  LOOP
    EXECUTE format('DROP INDEX IF EXISTS %s', target);
  END LOOP;
END
$$;
