-- 015 的回填已与旧数组逐题核对一致；子表成为唯一读取来源后删除旧列。
-- 若需回退，可用 jsonb_agg(payload ORDER BY case_index) 重新生成 results。
ALTER TABLE evaluations DROP COLUMN results;
