-- 评测结果改为逐题一行：更新一题只重写一行的 TOAST，而不是整个 results 数组。
-- 旧 evaluations.results 列暂时保留，等新代码验证后再由 016 删除。
CREATE TABLE evaluation_results (
 evaluation_id TEXT NOT NULL REFERENCES evaluations(evaluation_id) ON DELETE CASCADE,
 case_index INTEGER NOT NULL CHECK(case_index >= 0),
 payload JSONB NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(evaluation_id, case_index)
);

INSERT INTO evaluation_results(evaluation_id, case_index, payload, updated_at)
SELECT e.evaluation_id, (t.ordinality - 1)::int, t.value, e.updated_at
FROM evaluations e
CROSS JOIN LATERAL jsonb_array_elements(e.results) WITH ORDINALITY AS t(value, ordinality);
