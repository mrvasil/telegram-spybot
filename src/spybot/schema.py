"""Versioned SQLCipher schema. All content, including FTS, lives in the cipher DB."""

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE inbox (
 update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, received_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, last_error TEXT
);
CREATE INDEX inbox_pending ON inbox(state, retry_at, update_id);
CREATE TABLE applied_events (update_id INTEGER PRIMARY KEY);
CREATE TABLE connections (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, raw TEXT NOT NULL,
 is_enabled INTEGER NOT NULL, observed_at REAL NOT NULL
);
CREATE TABLE rejected_connections (id TEXT PRIMARY KEY);
CREATE TABLE messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 connection_id TEXT NOT NULL REFERENCES connections(id), chat_id INTEGER NOT NULL,
 message_id INTEGER NOT NULL, chat_title TEXT, sender_id INTEGER, sender_name TEXT,
 created_at REAL, deleted_at REAL, latest_version_id INTEGER,
 UNIQUE(connection_id, chat_id, message_id)
);
CREATE INDEX messages_chat ON messages(chat_id, id);
CREATE TABLE versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 message_fk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 event_update_id INTEGER NOT NULL, event_key TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN ('original','edit','context')),
 source_date REAL NOT NULL, observed_at REAL NOT NULL,
 text TEXT, caption TEXT, payload TEXT NOT NULL
);
CREATE INDEX versions_message ON versions(message_fk, source_date, event_update_id);
CREATE VIRTUAL TABLE version_search USING fts5(text, caption, content='versions', content_rowid='id');
CREATE TRIGGER versions_insert AFTER INSERT ON versions BEGIN
 INSERT INTO version_search(rowid,text,caption) VALUES(new.id,new.text,new.caption);
END;
CREATE TRIGGER versions_delete AFTER DELETE ON versions BEGIN
 INSERT INTO version_search(version_search,rowid,text,caption)
 VALUES('delete',old.id,old.text,old.caption);
END;
CREATE TRIGGER versions_update AFTER UPDATE OF text,caption ON versions BEGIN
 INSERT INTO version_search(version_search,rowid,text,caption)
 VALUES('delete',old.id,old.text,old.caption);
 INSERT INTO version_search(rowid,text,caption) VALUES(new.id,new.text,new.caption);
END;
CREATE TABLE deletion_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 message_fk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 event_update_id INTEGER NOT NULL, observed_at REAL NOT NULL, payload TEXT NOT NULL,
 UNIQUE(message_fk,event_update_id)
);
CREATE TABLE media (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
 file_id TEXT NOT NULL, file_unique_id TEXT, kind TEXT NOT NULL,
 metadata TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','saved','unavailable','too_large','failed')),
 object_path TEXT, sha256 TEXT, size INTEGER, error TEXT
);
CREATE INDEX media_version ON media(version_id);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('media','notification','export')),
 dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','running','done','failed','blocked','uncertain')),
 attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
 lease_until REAL, last_error TEXT
);
CREATE INDEX jobs_ready ON jobs(kind, state, available_at, id);
CREATE TABLE settings (scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(scope,key));
CREATE TABLE suppressions (
 connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 PRIMARY KEY(connection_id,chat_id,message_id)
);
"""
