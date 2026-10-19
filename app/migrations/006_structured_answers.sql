ALTER TABLE runs
 ADD COLUMN answer_status TEXT CHECK (answer_status IN ('answered','insufficient_evidence')),
 ADD COLUMN answer_reason TEXT CHECK (answer_reason IN
  ('missing_fact','conflicting_sources','no_retrieval_hits','no_context_fit')),
 ADD COLUMN answer_segments TEXT NOT NULL DEFAULT '[]',
 ADD COLUMN answer_protocol TEXT CHECK (answer_protocol = 'rag-answer-v2'),
 ADD COLUMN answer_repair_calls INTEGER NOT NULL DEFAULT 0 CHECK (answer_repair_calls BETWEEN 0 AND 1);
