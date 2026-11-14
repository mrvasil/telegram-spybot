"""Serialized, encrypted, durable storage for a single owner's Business archive."""

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import sqlcipher3

from spybot.normalize import chat_title, collect_media, contextual_messages, quote_search, sender
from spybot.schema import SCHEMA, SCHEMA_VERSION


class UnknownConnection(Exception):
    def __init__(self, connection_id: str):
        self.connection_id = connection_id
        super().__init__("unknown_business_connection")


class ForeignConnection(Exception):
    """The business connection does not belong to the configured owner."""


class IdentityMismatch(Exception):
    """This database is bound to another owner or bot."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _error_code(value: str) -> str:
    # Only structured codes may be persisted; never arbitrary exception text.
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value) else "unspecified_error"


class Archive:
    def __init__(self, db: aiosqlite.Connection, path: Path, key: bytes, owner_id: int):
        self._db = db
        self.path = path
        self._key = key
        self.owner_id = owner_id
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path, key: bytes, owner_id: int) -> "Archive":
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("archive_key_must_be_32_bytes")
        if not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id <= 0:
            raise ValueError("invalid_owner_id")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        def connector():
            connection = sqlcipher3.connect(str(path), isolation_level=None)
            connection.row_factory = sqlcipher3.Row
            return connection

        db = await aiosqlite.Connection(connector, iter_chunk_size=64)
        archive = cls(db, path, key, owner_id)
        try:
            await db.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
            cipher = await archive._one("PRAGMA cipher_version")
            if not cipher or not next(iter(cipher.values())):
                raise RuntimeError("sqlcipher_required")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA temp_store=MEMORY")
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=FULL")
            version_row = await archive._one("PRAGMA user_version")
            assert version_row is not None
            version = version_row["user_version"]
            if version > SCHEMA_VERSION:
                raise RuntimeError("unsupported_archive_schema")
            if version == 0:
                # executescript is a single explicit transaction, including user_version.
                await db.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + SCHEMA
                    + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
                )
            async with archive._transaction():
                await archive._bind_identity("owner_id", owner_id)
            return archive
        except BaseException:
            await db.close()
            raise

    async def close(self) -> None:
        async with self._lock:
            await self._db.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    @asynccontextmanager
    async def _transaction(self):
        async with self._lock:
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                yield
                await self._db.commit()
            except BaseException:
                # Includes cancellation; queued SQLite operations finish before rollback.
                await asyncio.shield(self._db.rollback())
                raise

    async def _one(self, sql: str, parameters=()) -> dict | None:
        async with self._db.execute(sql, parameters) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def _all(self, sql: str, parameters=()) -> list[dict]:
        async with self._db.execute(sql, parameters) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def _bind_identity(self, key: str, value: int) -> None:
        current = await self._one("SELECT value FROM meta WHERE key=?", (key,))
        if current and current["value"] != str(value):
            raise IdentityMismatch("archive_identity_mismatch")
        await self._db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (key, str(value))
        )

    async def bind_identity(self, bot_id: int) -> None:
        if not isinstance(bot_id, int) or isinstance(bot_id, bool) or bot_id <= 0:
            raise ValueError("invalid_bot_id")
        async with self._transaction():
            await self._bind_identity("bot_id", bot_id)

    async def ingest_batch(self, updates: list[dict]) -> int | None:
        async with self._transaction():
            highest = None
            suppressed = await self._suppression_set()
            for update in updates:
                update_id = update["update_id"]
                if not isinstance(update_id, int) or isinstance(update_id, bool):
                    raise ValueError("invalid_update_id")
                # Erasure removes inbox rows but retains event IDs; do not resurrect
                # their raw bodies on replay. New events are redacted only for an
                # explicitly erased identity; ordinary intake preserves every field.
                if not await self._one(
                    "SELECT 1 FROM applied_events WHERE update_id=?", (update_id,)
                ):
                    stored = _sanitize_payload(update, suppressed) if suppressed else update
                    await self._db.execute(
                        "INSERT OR IGNORE INTO inbox(update_id,payload,received_at) VALUES(?,?,?)",
                        (update_id, _json(stored), time.time()),
                    )
                highest = max(highest if highest is not None else update_id, update_id)
            current = await self._one("SELECT value FROM meta WHERE key='poll_offset'")
            offset = int(current["value"]) if current else None
            if highest is not None:
                offset = max(offset if offset is not None else highest + 1, highest + 1)
                await self._db.execute(
                    "INSERT INTO meta(key,value) VALUES('poll_offset',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(offset),),
                )
            return offset

    async def get_offset(self) -> int | None:
        async with self._lock:
            row = await self._one("SELECT value FROM meta WHERE key='poll_offset'")
            return int(row["value"]) if row else None

    async def pending_updates(self, limit: int = 100) -> list[dict]:
        async with self._lock:
            rows = await self._all(
                "SELECT update_id,payload,attempts FROM inbox WHERE state='pending' "
                "AND retry_at<=? ORDER BY update_id LIMIT ?",
                (time.time(), limit),
            )
            return [{**row, "payload": json.loads(row["payload"])} for row in rows]

    async def mark_processed(self, update_id: int, *, ignored: bool = False) -> None:
        async with self._transaction():
            await self._mark_processed(update_id, ignored=ignored)

    async def _mark_processed(self, update_id: int, *, ignored: bool = False) -> None:
        await self._db.execute(
            "UPDATE inbox SET state=?,last_error=NULL WHERE update_id=?",
            ("ignored" if ignored else "done", update_id),
        )

    async def fail_update(self, update_id: int, error_code: str, retry_at: float) -> None:
        async with self._transaction():
            await self._db.execute(
                "UPDATE inbox SET attempts=attempts+1,last_error=?,retry_at=? WHERE update_id=? AND state='pending'",
                (_error_code(error_code), retry_at, update_id),
            )

    async def connection(self, id: str) -> dict | None:
        async with self._lock:
            row = await self._one("SELECT * FROM connections WHERE id=?", (id,))
            return (
                {**row, "raw": json.loads(row["raw"]), "is_enabled": bool(row["is_enabled"])}
                if row
                else None
            )

    async def _remember_connection(self, raw: dict) -> bool:
        id = raw["id"]
        if not isinstance(id, str) or not id:
            raise ValueError("invalid_connection_id")
        owner = (raw.get("user") or {}).get("id")
        old = await self._one("SELECT owner_id FROM connections WHERE id=?", (id,))
        if old and old["owner_id"] != owner:
            raise IdentityMismatch("connection_identity_mismatch")
        if owner != self.owner_id:
            await self._db.execute(
                "INSERT OR IGNORE INTO rejected_connections(id) VALUES(?)", (id,)
            )
            return False
        await self._db.execute(
            "INSERT INTO connections(id,owner_id,raw,is_enabled,observed_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET raw=excluded.raw,is_enabled=excluded.is_enabled,observed_at=excluded.observed_at",
            (id, owner, _json(raw), bool(raw.get("is_enabled", False)), time.time()),
        )
        await self._db.execute("DELETE FROM rejected_connections WHERE id=?", (id,))
        return True

    async def remember_connection(self, raw: dict) -> None:
        async with self._transaction():
            accepted = await self._remember_connection(raw)
        if not accepted:
            raise ForeignConnection("foreign_business_connection")

    async def apply_update(self, update: dict, *, notify: bool = True) -> None:
        update_id = update["update_id"]
        async with self._transaction():
            if await self._one("SELECT 1 FROM applied_events WHERE update_id=?", (update_id,)):
                await self._mark_processed(update_id)
                return
            if "business_connection" in update:
                accepted = await self._remember_connection(update["business_connection"])
                await self._mark_processed(update_id, ignored=not accepted)
                await self._db.execute("INSERT INTO applied_events VALUES(?)", (update_id,))
                return
            key = next(
                (
                    k
                    for k in (
                        "business_message",
                        "edited_business_message",
                        "deleted_business_messages",
                    )
                    if k in update
                ),
                None,
            )
            if key is None:
                return  # Parent's owner UI decides how to process non-business updates.
            raw = _sanitize_payload(update[key], await self._suppression_set())
            connection_id = raw["business_connection_id"]
            if await self._one("SELECT 1 FROM rejected_connections WHERE id=?", (connection_id,)):
                await self._mark_processed(update_id, ignored=True)
                await self._db.execute("INSERT INTO applied_events VALUES(?)", (update_id,))
                return
            connection = await self._one("SELECT * FROM connections WHERE id=?", (connection_id,))
            if connection is None:
                raise UnknownConnection(connection_id)
            if connection["owner_id"] != self.owner_id:
                await self._mark_processed(update_id, ignored=True)
                return
            now = time.time()
            if key == "deleted_business_messages":
                for message_id in raw["message_ids"]:
                    internal_id = await self._message(connection_id, raw["chat"], message_id)
                    if internal_id is None:
                        continue
                    await self._db.execute(
                        "UPDATE messages SET deleted_at=COALESCE(deleted_at,?) WHERE id=?",
                        (now, internal_id),
                    )
                    await self._db.execute(
                        "INSERT OR IGNORE INTO deletion_events(message_fk,event_update_id,observed_at,payload) VALUES(?,?,?,?)",
                        (internal_id, update_id, now, _json(raw)),
                    )
                    if notify and await self._get_setting(
                        "notify_deletes", True, raw["chat"]["id"]
                    ):
                        await self._enqueue(
                            "notification",
                            f"delete:{update_id}:{internal_id}",
                            {"message_id": internal_id, "event": "delete"},
                        )
            else:
                kind = "edit" if key == "edited_business_message" else "original"
                result = await self._save_version(
                    connection_id, raw, kind, update_id, f"update:{update_id}", now
                )
                for path, context in contextual_messages(raw):
                    await self._save_version(
                        connection_id,
                        context,
                        "context",
                        update_id,
                        f"context:{update_id}:{path}",
                        now,
                    )
                if (
                    notify
                    and result
                    and kind == "edit"
                    and await self._get_setting("notify_edits", True, raw["chat"]["id"])
                ):
                    internal_id, version_id = result
                    await self._enqueue(
                        "notification",
                        f"edit:{update_id}",
                        {"message_id": internal_id, "event": "edit", "version_id": version_id},
                    )
            await self._db.execute("INSERT INTO applied_events VALUES(?)", (update_id,))
            await self._mark_processed(update_id)

    async def _suppression_set(self) -> set[tuple[str, int, int]]:
        rows = await self._all("SELECT connection_id,chat_id,message_id FROM suppressions")
        return {(r["connection_id"], r["chat_id"], r["message_id"]) for r in rows}

    async def _message(self, connection_id: str, chat: dict, message_id: int) -> int | None:
        identity = (connection_id, chat["id"], message_id)
        if await self._one(
            "SELECT 1 FROM suppressions WHERE connection_id=? AND chat_id=? AND message_id=?",
            identity,
        ):
            return None
        await self._db.execute(
            "INSERT OR IGNORE INTO messages(connection_id,chat_id,message_id,chat_title) VALUES(?,?,?,?)",
            (*identity, chat_title(chat)),
        )
        row = await self._one(
            "SELECT id FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?", identity
        )
        assert row is not None
        return row["id"]

    async def _save_version(
        self, connection_id: str, raw: dict, kind: str, update_id: int, event_key: str, now: float
    ) -> tuple[int, int] | None:
        internal_id = await self._message(connection_id, raw["chat"], raw["message_id"])
        if internal_id is None:
            return None
        source_date = raw.get("edit_date") or raw.get("date") or 0
        cursor = await self._db.execute(
            "INSERT INTO versions(message_fk,event_update_id,event_key,kind,source_date,observed_at,text,caption,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                internal_id,
                update_id,
                event_key,
                kind,
                source_date,
                now,
                raw.get("text"),
                raw.get("caption"),
                _json(raw),
            ),
        )
        version_id = cursor.lastrowid
        assert version_id is not None
        await cursor.close()
        old = await self._one(
            "SELECT v.* FROM messages m JOIN versions v ON v.id=m.latest_version_id WHERE m.id=?",
            (internal_id,),
        )
        rank = (kind != "context", source_date, update_id)
        if old is None or (
            not (old["kind"] == "edit" and kind == "original")
            and rank > (old["kind"] != "context", old["source_date"], old["event_update_id"])
        ):
            sender_id, sender_name = sender(raw)
            await self._db.execute(
                "UPDATE messages SET latest_version_id=?,chat_title=COALESCE(?,chat_title),sender_id=?,sender_name=? WHERE id=?",
                (version_id, chat_title(raw["chat"]), sender_id, sender_name, internal_id),
            )
        await self._db.execute(
            "UPDATE messages SET created_at=CASE WHEN created_at IS NULL THEN ? ELSE MIN(created_at,?) END WHERE id=?",
            (raw.get("date", source_date), raw.get("date", source_date), internal_id),
        )
        for item in collect_media(raw, skip_context=True):
            cursor = await self._db.execute(
                "INSERT INTO media(version_id,file_id,file_unique_id,kind,metadata) VALUES(?,?,?,?,?)",
                (
                    version_id,
                    item["file_id"],
                    item["file_unique_id"],
                    item["kind"],
                    _json(item["metadata"]),
                ),
            )
            media_id = cursor.lastrowid
            await cursor.close()
            await self._enqueue(
                "media", f"media:{media_id}", {"media_id": media_id, "file_id": item["file_id"]}
            )
        return internal_id, version_id

    async def _enqueue(self, kind: str, key: str, payload: dict) -> int:
        await self._db.execute(
            "INSERT OR IGNORE INTO jobs(kind,dedupe_key,payload,available_at) VALUES(?,?,?,?)",
            (kind, key, _json(payload), time.time()),
        )
        row = await self._one("SELECT id FROM jobs WHERE dedupe_key=?", (key,))
        assert row is not None
        return row["id"]

    async def enqueue(self, kind: str, key: str, payload: dict) -> int:
        async with self._transaction():
            return await self._enqueue(kind, key, payload)

    async def claim_job(self, kind: str, lease_seconds: float = 300) -> dict | None:
        if lease_seconds <= 0:
            raise ValueError("lease_must_be_positive")
        async with self._transaction():
            now = time.time()
            # An expired send may have reached Telegram. Never automatically duplicate it.
            await self._db.execute(
                "UPDATE jobs SET state=CASE WHEN kind='notification' THEN 'uncertain' ELSE 'pending' END,lease_until=NULL,last_error='lease_expired' "
                "WHERE state='running' AND lease_until<=?",
                (now,),
            )
            row = await self._one(
                "SELECT * FROM jobs WHERE kind=? AND state='pending' AND available_at<=? ORDER BY id LIMIT 1",
                (kind, now),
            )
            if not row:
                return None
            await self._db.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,lease_until=? WHERE id=?",
                (now + lease_seconds, row["id"]),
            )
            return {
                **row,
                "state": "running",
                "attempts": row["attempts"] + 1,
                "lease_until": now + lease_seconds,
                "payload": json.loads(row["payload"]),
            }

    async def finish_job(self, id: int) -> None:
        async with self._transaction():
            await self._db.execute(
                "UPDATE jobs SET state='done',lease_until=NULL,last_error=NULL WHERE id=?", (id,)
            )

    async def retry_job(
        self,
        id: int,
        error_code: str,
        delay: float,
        terminal: bool = False,
        state: str | None = None,
    ) -> None:
        target = state or ("failed" if terminal else "pending")
        if target not in {"pending", "failed", "blocked", "uncertain"}:
            raise ValueError("invalid_retry_state")
        async with self._transaction():
            await self._db.execute(
                "UPDATE jobs SET state=?,available_at=?,lease_until=NULL,last_error=? WHERE id=?",
                (target, time.time() + max(0, delay), _error_code(error_code), id),
            )

    async def media(self, id: int) -> dict | None:
        async with self._lock:
            row = await self._one("SELECT * FROM media WHERE id=?", (id,))
            return {**row, "metadata": json.loads(row["metadata"])} if row else None

    async def update_media(self, id: int, **allowed_fields) -> bool:
        allowed = {"state", "object_path", "sha256", "size", "error"}
        if not allowed_fields or set(allowed_fields) - allowed:
            raise ValueError("invalid_media_fields")
        if allowed_fields.get("error") is not None:
            allowed_fields["error"] = _error_code(allowed_fields["error"])
        async with self._transaction():
            cursor = await self._db.execute(
                f"UPDATE media SET {','.join(k + '=?' for k in allowed_fields)} WHERE id=?",
                (*allowed_fields.values(), id),
            )
            changed = cursor.rowcount > 0
            await cursor.close()
            return changed

    async def object_paths(self) -> set[str]:
        async with self._lock:
            rows = await self._all(
                "SELECT DISTINCT object_path FROM media WHERE object_path IS NOT NULL"
            )
            return {row["object_path"] for row in rows}

    async def _message_media(self, message_internal_id: int) -> list[dict]:
        rows = await self._all(
            "SELECT d.* FROM media d JOIN versions v ON v.id=d.version_id WHERE v.message_fk=? ORDER BY d.id",
            (message_internal_id,),
        )
        return [{**row, "metadata": json.loads(row["metadata"])} for row in rows]

    async def message_media(self, message_internal_id: int) -> list[dict]:
        async with self._lock:
            return await self._message_media(message_internal_id)

    async def _get_setting(self, key: str, default=None, chat_id: int | None = None):
        if chat_id is not None:
            row = await self._one(
                "SELECT value FROM settings WHERE scope=? AND key=?", (str(chat_id), key)
            )
            if row:
                return json.loads(row["value"])
        row = await self._one("SELECT value FROM settings WHERE scope='global' AND key=?", (key,))
        return json.loads(row["value"]) if row else default

    async def get_setting(self, key: str, default=None, chat_id: int | None = None):
        async with self._lock:
            return await self._get_setting(key, default, chat_id)

    async def set_setting(self, key: str, value, chat_id: int | None = None) -> None:
        async with self._transaction():
            await self._db.execute(
                "INSERT INTO settings(scope,key,value) VALUES(?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value",
                ("global" if chat_id is None else str(chat_id), key, _json(value)),
            )

    async def stats(self) -> dict:
        async with self._lock:
            row = await self._one("""SELECT
              (SELECT COUNT(*) FROM messages) AS messages,
              (SELECT COUNT(*) FROM versions) AS versions,
              (SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL) AS deleted,
              (SELECT COUNT(*) FROM media) AS media,
              (SELECT COUNT(*) FROM media WHERE state='pending') AS pending_media,
              (SELECT COUNT(*) FROM inbox WHERE state='pending') AS inbox_pending,
              (SELECT COUNT(*) FROM jobs WHERE state IN ('pending','running')) AS jobs_pending""")
            assert row is not None
            return row

    async def list_chats(self, limit: int = 20, offset: int = 0) -> list[dict]:
        async with self._lock:
            return await self._all(
                "SELECT connection_id,chat_id,MAX(chat_title) AS title,COUNT(*) AS messages FROM messages GROUP BY connection_id,chat_id ORDER BY MAX(id) DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )

    async def list_messages(
        self,
        limit: int = 20,
        offset: int = 0,
        chat_id: int | None = None,
        deleted_only: bool = False,
    ) -> list[dict]:
        async with self._lock:
            return await self._all(
                "SELECT m.*,v.text,v.caption,v.source_date,v.kind FROM messages m LEFT JOIN versions v ON v.id=m.latest_version_id "
                "WHERE (? IS NULL OR m.chat_id=?) AND (?=0 OR m.deleted_at IS NOT NULL) ORDER BY m.id DESC LIMIT ? OFFSET ?",
                (chat_id, chat_id, deleted_only, limit, offset),
            )

    async def history(self, internal_id: int) -> dict | None:
        async with self._lock:
            message = await self._one("SELECT * FROM messages WHERE id=?", (internal_id,))
            if message is None:
                return None
            versions = await self._all(
                "SELECT * FROM versions WHERE message_fk=? ORDER BY source_date,event_update_id,id",
                (internal_id,),
            )
            deletions = await self._all(
                "SELECT * FROM deletion_events WHERE message_fk=? ORDER BY event_update_id",
                (internal_id,),
            )
            return {
                "message": message,
                "versions": [{**v, "payload": json.loads(v["payload"])} for v in versions],
                "deletion_events": [{**v, "payload": json.loads(v["payload"])} for v in deletions],
                "media": await self._message_media(internal_id),
            }

    async def search(
        self, query: str, limit: int = 20, offset: int = 0, chat_id: int | None = None
    ) -> list[dict]:
        phrase = quote_search(query)
        if not phrase:
            return []
        async with self._lock:
            return await self._all(
                "SELECT m.id AS message_id,v.id AS version_id,v.text,v.caption,m.chat_id,v.source_date "
                "FROM version_search JOIN versions v ON v.id=version_search.rowid JOIN messages m ON m.id=v.message_fk "
                "WHERE version_search MATCH ? AND (? IS NULL OR m.chat_id=?) ORDER BY rank,v.id DESC LIMIT ? OFFSET ?",
                (phrase, chat_id, chat_id, limit, offset),
            )

    async def erase_message(self, id: int) -> list[str]:
        async with self._transaction():
            return await self._erase_message(id)

    async def _erase_message(self, id: int) -> list[str]:
        message = await self._one("SELECT * FROM messages WHERE id=?", (id,))
        if message is None:
            return []
        identity = (message["connection_id"], message["chat_id"], message["message_id"])
        await self._db.execute("INSERT OR IGNORE INTO suppressions VALUES(?,?,?)", identity)
        paths = [r["object_path"] for r in await self._message_media(id) if r["object_path"]]
        await self._db.execute(
            "DELETE FROM jobs WHERE (kind='media' AND json_extract(payload,'$.media_id') IN "
            "(SELECT d.id FROM media d JOIN versions v ON v.id=d.version_id WHERE v.message_fk=?)) "
            "OR (kind IN ('notification','export') AND json_extract(payload,'$.message_id')=?)",
            (id, id),
        )
        # Remove any retained inbox event carrying this message, including a nested snapshot.
        rows = await self._all("SELECT update_id,payload FROM inbox")
        for row in rows:
            if _contains_message(json.loads(row["payload"]), *identity):
                await self._db.execute("DELETE FROM inbox WHERE update_id=?", (row["update_id"],))
        # Redact embedded copies in other versions; keep their unrelated content.
        for row in await self._all("SELECT id,payload FROM versions WHERE message_fk<>?", (id,)):
            payload = json.loads(row["payload"])
            if _redact_message(payload, *identity):
                await self._db.execute(
                    "UPDATE versions SET payload=? WHERE id=?", (_json(payload), row["id"])
                )
        await self._db.execute("DELETE FROM messages WHERE id=?", (id,))
        return list(dict.fromkeys(paths))

    async def prune(self, before_timestamp: float) -> list[str]:
        """Explicit destructive retention; never called automatically by the archive."""
        async with self._transaction():
            rows = await self._all(
                "SELECT id FROM messages WHERE COALESCE(created_at,deleted_at)<?",
                (before_timestamp,),
            )
            paths = []
            for row in rows:
                paths.extend(await self._erase_message(row["id"]))
            return list(dict.fromkeys(paths))

    async def backup_to(self, path: Path) -> None:
        """Write a consistent encrypted snapshot; fail if the destination exists."""
        path = Path(path)
        if path.exists() or path.resolve() == self.path.resolve():
            raise ValueError("backup_destination_exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)

            def backup():
                target = sqlcipher3.connect(str(path), isolation_level=None)
                try:
                    target.execute(f"PRAGMA key = \"x'{self._key.hex()}'\"")
                    self._db._conn.backup(target)
                    target.execute("SELECT COUNT(*) FROM meta").fetchone()
                finally:
                    target.close()

            # Both source and target native connections stay on the aiosqlite worker.
            await self._db._execute(backup)


def _contains_message(
    value: Any,
    connection_id: str,
    chat_id: int,
    message_id: int,
    inherited_connection: str | None = None,
) -> bool:
    if isinstance(value, dict):
        connection = value.get("business_connection_id", inherited_connection)
        if connection == connection_id and (value.get("chat") or {}).get("id") == chat_id:
            if value.get("message_id") == message_id or message_id in value.get("message_ids", []):
                return True
        return any(
            _contains_message(v, connection_id, chat_id, message_id, connection)
            for v in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_message(v, connection_id, chat_id, message_id, inherited_connection)
            for v in value
        )
    return False


def _redact_message(
    value: Any,
    connection_id: str,
    chat_id: int,
    message_id: int,
    inherited_connection: str | None = None,
) -> bool:
    changed = False
    if isinstance(value, dict):
        connection = value.get("business_connection_id", inherited_connection)
        for key, item in list(value.items()):
            if (
                isinstance(item, dict)
                and connection == connection_id
                and (item.get("chat") or {}).get("id") == chat_id
                and item.get("message_id") == message_id
            ):
                value[key] = {
                    "message_id": message_id,
                    "chat": {"id": chat_id},
                    "archive_erased": True,
                }
                changed = True
            else:
                changed = (
                    _redact_message(item, connection_id, chat_id, message_id, connection) or changed
                )
    elif isinstance(value, list):
        for item in value:
            changed = (
                _redact_message(item, connection_id, chat_id, message_id, inherited_connection)
                or changed
            )
    return changed


def _sanitize_payload(
    value: Any, suppressed: set[tuple[str, int, int]], inherited_connection: str | None = None
) -> Any:
    if isinstance(value, dict):
        connection = value.get("business_connection_id", inherited_connection)
        chat = value.get("chat") or {}
        if (connection, chat.get("id"), value.get("message_id")) in suppressed:
            redacted = {
                "message_id": value["message_id"],
                "chat": {"id": chat["id"]},
                "archive_erased": True,
            }
            if "business_connection_id" in value:
                redacted["business_connection_id"] = connection
            return redacted
        result = {k: _sanitize_payload(v, suppressed, connection) for k, v in value.items()}
        if "message_ids" in value and isinstance(value["message_ids"], list):
            result["message_ids"] = [
                i for i in value["message_ids"] if (connection, chat.get("id"), i) not in suppressed
            ]
        return result
    if isinstance(value, list):
        return [_sanitize_payload(v, suppressed, inherited_connection) for v in value]
    return value
