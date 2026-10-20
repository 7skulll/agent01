ALTER TABLE runs ADD COLUMN evaluation_options JSONB;
ALTER TABLE evaluations ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE evaluations ADD COLUMN owned_conversation_ids JSONB NOT NULL DEFAULT '[]';
UPDATE evaluations e SET owned_conversation_ids=COALESCE(
 (SELECT jsonb_agg(DISTINCT item->'run'->>'conversation_id')
  FROM jsonb_array_elements(e.results) item
  WHERE item->'run'->>'conversation_id' IS NOT NULL), '[]'::jsonb);
ALTER TABLE evaluation_case_sets ADD COLUMN split TEXT NOT NULL DEFAULT 'development'
 CHECK(split IN ('development','acceptance'));
ALTER TABLE evaluation_case_sets ADD COLUMN review_status TEXT NOT NULL DEFAULT 'draft'
 CHECK(review_status IN ('draft','verified'));
ALTER TABLE evaluation_case_sets ADD COLUMN frozen BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX evaluations_history ON evaluations ((request->>'knowledge_base_id'),archived,created_at DESC,evaluation_id);
