-- 测评集归档：重复或历史版本可以保留但默认不出现在列表里。
ALTER TABLE evaluation_case_sets ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX evaluation_case_sets_active ON evaluation_case_sets(kb_id, archived, created_at DESC);
