PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS apify_token_status (
  token_hash TEXT PRIMARY KEY,
  token_label TEXT NOT NULL DEFAULT '',
  owner_email TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspect','broken')),
  last_error TEXT NOT NULL DEFAULT '',
  last_error_type TEXT NOT NULL DEFAULT '',
  last_error_at REAL,
  last_success_at REAL,
  fail_count INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS token_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL,
  owner_email TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT '',
  error_type TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  first_seen_at REAL NOT NULL,
  sent_at REAL,
  acked_at REAL,
  reminders_sent INTEGER NOT NULL DEFAULT 0,
  next_reminder_at REAL,
  done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_token_alerts_open ON token_alerts(done, next_reminder_at);
CREATE TABLE IF NOT EXISTS bookmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  file_id TEXT NOT NULL DEFAULT '',
  media_kind TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id, id DESC);
CREATE TABLE IF NOT EXISTS user_download_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  media_kind TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  request_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user_time ON user_download_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_platform ON user_download_events(platform);
CREATE TABLE IF NOT EXISTS media_dedupe (
  fingerprint TEXT PRIMARY KEY,
  source_url TEXT NOT NULL DEFAULT '',
  quality TEXT NOT NULL DEFAULT '',
  file_id TEXT NOT NULL DEFAULT '',
  mime_type TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  last_hit_at REAL NOT NULL,
  hits INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS autoshare_targets (
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  added_at REAL NOT NULL,
  PRIMARY KEY (user_id, chat_id)
);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  interval_minutes INTEGER NOT NULL DEFAULT 10080,
  next_run_at REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  last_status TEXT NOT NULL DEFAULT '',
  last_run_at REAL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs(active, next_run_at);
CREATE TABLE IF NOT EXISTS size_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  quality TEXT NOT NULL DEFAULT '',
  expected_bytes INTEGER,
  actual_bytes INTEGER,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_cache (
  cache_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
