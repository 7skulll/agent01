CREATE TABLE users (
 user_id TEXT PRIMARY KEY, username TEXT NOT NULL, display_name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','staff','viewer')), password_hash TEXT NOT NULL,
 is_active BOOLEAN NOT NULL DEFAULT TRUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 last_login_at TEXT
);
-- 用户名不区分大小写唯一；登录查询同样按 lower(username) 匹配。
CREATE UNIQUE INDEX users_username_ci ON users (lower(username));

CREATE TABLE user_sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE INDEX user_sessions_user ON user_sessions(user_id);
CREATE INDEX user_sessions_expiry ON user_sessions(expires_at);
