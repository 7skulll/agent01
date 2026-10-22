-- 文件级聚合随全文索引原子提交；查询不再物化全知识库的 chunk_terms。
ALTER TABLE lexical_indexes ADD COLUMN chunk_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE lexical_indexes ADD COLUMN token_count BIGINT NOT NULL DEFAULT 0;
UPDATE lexical_indexes l SET chunk_count=s.n,token_count=s.tokens FROM (
 SELECT file_id,count(*) AS n,sum(token_count) AS tokens FROM chunk_terms GROUP BY file_id
) AS s WHERE s.file_id=l.file_id;

-- (term,chunk_id) 主键已支持词项查找；避免再建重复的 posting 索引。
CREATE INDEX terms_kb ON chunk_terms(kb_id,chunk_id) INCLUDE (token_count);

-- 汉字双字词项不依赖 Jieba 边界；ASCII 标识保留完整拼写与标点。
CREATE FUNCTION rag_character_terms(input TEXT) RETURNS TEXT[]
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
 SELECT COALESCE(array_agg(DISTINCT term ORDER BY term),ARRAY[]::text[]) FROM (
   SELECT CASE WHEN word ~ '^[㐀-鿿]+$' THEN substr(word,pos,2) ELSE word END AS term
   FROM (SELECT m[1] AS word FROM regexp_matches(lower(input),
       '[㐀-鿿]+|[a-z0-9]+(?:[-_.][a-z0-9]+)*','g') AS m) AS words
   CROSS JOIN LATERAL generate_series(1,
       CASE WHEN word ~ '^[㐀-鿿]+$' THEN length(word)-1 ELSE 1 END) AS pos
 ) AS terms
$$;
ALTER TABLE chunks ADD COLUMN character_terms TEXT[] GENERATED ALWAYS AS
 (rag_character_terms(COALESCE(heading,'') || E'\n' || content)) STORED;
CREATE INDEX chunks_character_terms ON chunks USING gin(character_terms);
