-- 默认值兼容既有任务；执行状态同时写入 checkpoint，重启和恢复沿用原检索模式。
ALTER TABLE runs ADD COLUMN retrieval_mode TEXT NOT NULL DEFAULT 'hybrid'
    CHECK (retrieval_mode IN ('hybrid', 'keyword', 'vector'));
