"""Read-only, repeatable import from the original single-owner SQLite schema."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from spybot.archive import Archive
from spybot.async_utils import filesystem_call
from spybot.config import Settings
from spybot.crypto import encrypt_file, fsync_directory, verify_object
from spybot.media import object_path


def _event_id(source_digest: str, identity: str) -> int:
    digest = hashlib.sha256(f"{source_digest}:{identity}".encode()).digest()
    return -int.from_bytes(digest[:7], "big") - 1


def _timestamp(value: str | None, timezone: str | None) -> int:
    if not value:
        return 0
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        if timezone is None:
            raise ValueError("Для старых дат без часового пояса укажите --source-timezone")
        date = date.replace(tzinfo=ZoneInfo(timezone))
    return int(date.timestamp())


def _source_digest(source: Path) -> str:
    with source.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def import_legacy(
    archive: Archive,
    settings: Settings,
    key: bytes,
    source: Path,
    media_root: Path,
    source_timezone: str | None,
    *,
    apply: bool = False,
) -> dict:
    source = source.resolve()
    media_root = media_root.resolve()
    if source == archive.path.resolve():
        raise ValueError("Нельзя импортировать архив в него самого")
    if not source.is_file():
        raise ValueError("Не найдена исходная SQLite")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError(
                "Остановите старую установку и сделайте согласованную копию SQLite перед импортом"
            )
    digest = await filesystem_call(_source_digest, source)
    connection_id = f"legacy:{digest}"
    connection = sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    report: dict[str, Any] = {
        "dry_run": not apply,
        "source_sha256": digest,
        "source_timezone": source_timezone,
        "messages": 0,
        "actions": 0,
        "media_records": 0,
        "available_files": 0,
        "missing_files": 0,
        "unsafe_paths": 0,
        "naive_dates": 0,
        "orphan_actions": 0,
        "orphan_media": 0,
    }
    try:
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"messages", "users", "message_actions", "media_files"}
        if not required <= tables:
            raise ValueError("Это не база старой версии telegram-spybot")
        report["messages"] = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        report["actions"] = connection.execute("SELECT COUNT(*) FROM message_actions").fetchone()[0]
        report["media_records"] = connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[
            0
        ]
        report["orphan_actions"] = connection.execute(
            "SELECT COUNT(*) FROM message_actions a LEFT JOIN messages m ON a.chat_id=m.chat_id AND a.message_id=m.message_id WHERE m.chat_id IS NULL"
        ).fetchone()[0]
        report["orphan_media"] = connection.execute(
            "SELECT COUNT(*) FROM media_files a LEFT JOIN messages m ON a.chat_id=m.chat_id AND a.message_id=m.message_id WHERE m.chat_id IS NULL"
        ).fetchone()[0]
        for row in connection.execute(
            "SELECT date AS value FROM messages UNION ALL SELECT action_date AS value FROM message_actions"
        ):
            if row["value"] and datetime.fromisoformat(row["value"]).tzinfo is None:
                report["naive_dates"] += 1
        if apply and report["naive_dates"] and source_timezone is None:
            raise ValueError("Для старых дат без часового пояса укажите --source-timezone")
        file_paths = {}
        for row in connection.execute("SELECT id,media_path FROM media_files"):
            raw_path = Path(row["media_path"] or "")
            candidate = (raw_path if raw_path.is_absolute() else media_root / raw_path).resolve()
            if media_root not in candidate.parents:
                report["unsafe_paths"] += 1
            elif candidate.is_file():
                report["available_files"] += 1
                file_paths[row["id"]] = candidate
            else:
                report["missing_files"] += 1
        if not apply:
            return report
        await archive.remember_connection(
            {
                "id": connection_id,
                "user": {"id": settings.owner_id},
                "is_enabled": False,
                "legacy_import": {"source_sha256": digest, "timezone": source_timezone},
            }
        )
        for record in connection.execute(
            "SELECT m.*,u.username FROM messages m LEFT JOIN users u ON u.id=m.user_id ORDER BY m.chat_id,m.message_id"
        ):
            row = dict(record)
            actions = [
                dict(a)
                for a in connection.execute(
                    "SELECT * FROM message_actions WHERE chat_id=? AND message_id=? ORDER BY action_date,id",
                    (row["chat_id"], row["message_id"]),
                )
            ]
            files = [
                dict(f)
                for f in connection.execute(
                    "SELECT * FROM media_files WHERE chat_id=? AND message_id=? ORDER BY id",
                    (row["chat_id"], row["message_id"]),
                )
            ]
            first_edit = next((a for a in actions if a["action_type"] == "edit"), None)
            raw = {
                "business_connection_id": connection_id,
                "chat": {
                    "id": row["chat_id"],
                    "type": "private",
                    "first_name": row.get("username") or str(row["chat_id"]),
                },
                "message_id": row["message_id"],
                "date": _timestamp(row["date"], source_timezone),
                "text": (first_edit["old_text"] if first_edit else row["text"]) or "",
                "legacy_import": {"source_sha256": digest, "original": row, "media": files},
            }
            if row.get("user_id"):
                raw["from"] = {"id": row["user_id"], "username": row.get("username")}
            # Keep old media file IDs once, not again in every synthetic edit version.
            # Original rows are retained intact for forensic interpretation.
            update_id = _event_id(digest, f"message:{row['chat_id']}:{row['message_id']}")
            await archive.apply_update(
                {"update_id": update_id, "business_message": raw}, notify=False
            )
            for action in actions:
                action_id = _event_id(digest, f"action:{action['id']}")
                if action["action_type"] == "edit":
                    edited = {
                        **raw,
                        "text": action["new_text"] or "",
                        "edit_date": _timestamp(action["action_date"], source_timezone),
                        "legacy_import": {"source_sha256": digest, "action": action},
                    }
                    await archive.apply_update(
                        {"update_id": action_id, "edited_business_message": edited}, notify=False
                    )
                elif action["action_type"] == "delete":
                    deleted = {
                        "business_connection_id": connection_id,
                        "chat": raw["chat"],
                        "message_ids": [row["message_id"]],
                        "legacy_import": {"source_sha256": digest, "action": action},
                    }
                    await archive.apply_update(
                        {"update_id": action_id, "deleted_business_messages": deleted}, notify=False
                    )
            # Original media may already be gone; keep file IDs and honest status either way.
        # Match imported records once by source file ID+source row id path metadata.
        offset = 0
        while messages := await archive.list_messages(limit=100, offset=offset):
            offset += len(messages)
            for message in messages:
                if message["connection_id"] != connection_id:
                    continue
                for media in await archive.message_media(message["id"]):
                    original = media["metadata"].get("file", {})
                    path = file_paths.get(original.get("id"))
                    if path is None or media["state"] == "saved":
                        continue
                    staging = settings.data_dir / "staging" / f"{uuid4().hex}.part"
                    checksum, size = await filesystem_call(encrypt_file, path, staging, key)
                    relative = f"objects/{checksum[:2]}/{checksum}.enc"
                    destination = object_path(settings.data_dir, relative)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if destination.exists():
                        if await filesystem_call(verify_object, destination, key) != (
                            checksum,
                            size,
                        ):
                            raise ValueError("Existing media checksum mismatch")
                        staging.unlink()
                    else:
                        os.replace(staging, destination)
                        fsync_directory(destination.parent)
                    await archive.update_media(
                        media["id"],
                        state="saved",
                        object_path=relative,
                        sha256=checksum,
                        size=size,
                        error=None,
                    )
                    # This explicit offline import owns the DB lock; complete only its media job.
                    async with archive._transaction():
                        await archive._db.execute(
                            "UPDATE jobs SET state='done',lease_until=NULL WHERE dedupe_key=?",
                            (f"media:{media['id']}",),
                        )
        if "settings" in tables:
            for row in connection.execute("SELECT key,value FROM settings"):
                mapped = {"notify_edited": "notify_edits", "notify_deleted": "notify_deletes"}.get(
                    row["key"]
                )
                if mapped and await archive.get_setting(mapped) is None:
                    await archive.set_setting(mapped, bool(row["value"]))
                if (
                    row["key"] == "ignore_changes_below"
                    and await archive.get_setting("ignore_changes_below") is None
                ):
                    await archive.set_setting("ignore_changes_below", int(row["value"]))
                if row["key"].startswith("notify_user_"):
                    # Legacy setting was keyed by sender, not chat; preserve it as provenance.
                    await archive.set_setting("legacy_" + row["key"], bool(row["value"]))
        report["archive_stats"] = await archive.stats()
        return report
    finally:
        connection.close()
