CREATE TABLE model_settings (
 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
 payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
