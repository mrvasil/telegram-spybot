"""Encrypted snapshots, portable exports and deliberate archive erasure."""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import shutil
import time
import zipfile
from collections.abc import Buffer
from pathlib import Path
from typing import Any
from uuid import uuid4

from spybot.archive import Archive
from spybot.async_utils import filesystem_call
from spybot.config import Settings
from spybot.crypto import (
    EncryptedReader,
    EncryptedWriter,
    decrypt_chunks,
    fsync_directory,
    verify_object,
)
from spybot.media import object_path

logger = logging.getLogger(__name__)

_EXPORT_HEARTBEAT_SECONDS = 60.0


class _EncryptedSink(io.RawIOBase):
    def __init__(self, writer: EncryptedWriter):
        self.writer = writer

    def write(self, data: Buffer) -> int:
        content = bytes(data)
        self.writer.write(content)
        return len(content)

    def tell(self) -> int:
        return self.writer.size

    def flush(self) -> None:
        pass


def _hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _pack_backup(
    snapshot: Path, paths: list[Path], data_dir: Path, destination: Path, key: bytes, stats: dict
) -> None:
    writer = EncryptedWriter(destination, key)
    try:
        with zipfile.ZipFile(
            _EncryptedSink(writer), "w", compression=zipfile.ZIP_STORED
        ) as archive:
            manifest: dict[str, Any] = {
                "format": 1,
                "created_at": time.time(),
                "stats": stats,
                "files": {},
            }
            for path in [snapshot, *paths]:
                name = "archive.db" if path == snapshot else str(path.relative_to(data_dir))
                manifest["files"][name] = {"sha256": _hash(path), "bytes": path.stat().st_size}
                archive.write(path, name)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        writer.finish()
    except BaseException:
        writer.abort()
        raise


def _write_decrypted_file(output, encrypted: Path, key: bytes) -> None:
    for chunk in decrypt_chunks(encrypted, key):
        output.write(chunk)


def restore_backup(
    source: Path, destination: Path, key: bytes, max_bytes: int = 100 * 1024**3
) -> dict:
    """Restore only into a new directory. Never overwrite an existing/live archive."""
    if destination.exists():
        raise ValueError("Каталог восстановления уже существует")
    if max_bytes <= 0:
        raise ValueError("Недопустимый предел восстановления")
    destination.mkdir(parents=True, mode=0o700)
    try:
        with EncryptedReader(source, key) as reader, zipfile.ZipFile(reader) as archive:
            info = archive.getinfo("manifest.json")
            if info.file_size > 16 * 1024 * 1024:
                raise ValueError("Backup manifest too large")
            manifest = json.loads(archive.read(info))
            if manifest.get("format") != 1 or "archive.db" not in manifest.get("files", {}):
                raise ValueError("Unsupported backup format")
            names = [i.filename for i in archive.infolist()]
            if len(names) != len(set(names)) or set(names) != set(manifest["files"]) | {
                "manifest.json"
            }:
                raise ValueError("Backup entries do not match manifest")
            total = sum(i.file_size for i in archive.infolist())
            if total > max_bytes or total > shutil.disk_usage(destination).free:
                raise ValueError("Insufficient restore space or restore limit exceeded")
            for name, metadata in manifest["files"].items():
                if name != "archive.db" and not name.startswith("objects/"):
                    raise ValueError("Unexpected backup path")
                path = (destination / name).resolve()
                if (
                    destination.resolve() not in path.parents
                    or ".." in Path(name).parts
                    or Path(name).is_absolute()
                ):
                    raise ValueError("Unsafe backup path")
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                count = 0
                with archive.open(name) as entry, path.open("xb") as output:
                    os.chmod(path, 0o600)
                    while chunk := entry.read(256 * 1024):
                        count += len(chunk)
                        if count > metadata["bytes"]:
                            raise ValueError("Backup file exceeds manifest size")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if count != metadata["bytes"] or digest.hexdigest() != metadata["sha256"]:
                    raise ValueError("Backup checksum mismatch")
                if name.startswith("objects/"):
                    verify_object(path, key)
        fsync_directory(destination)
        return manifest
    except BaseException:
        shutil.rmtree(destination)
        raise


class Operations:
    def __init__(self, archive: Archive, settings: Settings, key: bytes, lock: asyncio.Lock):
        self.archive = archive
        self.settings = settings
        self.key = key
        self.lock = lock

    async def _snapshot(self) -> tuple[Path, Archive]:
        path = self.settings.data_dir / "staging" / f"{uuid4().hex}.snapshot"
        backup = asyncio.create_task(self.archive.backup_to(path))
        try:
            try:
                await asyncio.shield(backup)
            except asyncio.CancelledError:
                # Native backup runs on aiosqlite's worker and cannot be interrupted.
                # Finish it before cleanup, otherwise it may recreate the removed file.
                try:
                    await backup
                except Exception:
                    pass
                raise
            snapshot = await Archive.open(path, self.key, self.settings.owner_id)
        except BaseException:
            self._remove_snapshot(path)
            raise
        return path, snapshot

    @staticmethod
    def _remove_snapshot(path: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    @staticmethod
    async def _close_snapshot(path: Path, snapshot: Archive) -> None:
        try:
            await snapshot.close()
        finally:
            Operations._remove_snapshot(path)

    async def backup(self) -> Path:
        async with self.lock:
            path, snapshot = await self._snapshot()
            destination = (
                self.settings.data_dir
                / "backups"
                / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid4().hex[:8]}.sba"
            )
            try:
                paths = [
                    object_path(self.settings.data_dir, p)
                    for p in sorted(await snapshot.object_paths())
                ]
                if any(not p.is_file() for p in paths):
                    raise ValueError("Backup contains missing media; repair required")
                estimate = path.stat().st_size + sum(p.stat().st_size for p in paths)
                if (
                    estimate + self.settings.min_free_bytes
                    > shutil.disk_usage(self.settings.data_dir).free
                ):
                    raise OSError("Insufficient backup space")
                stats = await snapshot.stats()
                partial = self.settings.data_dir / "staging" / f"{uuid4().hex}.backup-part"
                try:
                    await filesystem_call(
                        _pack_backup, path, paths, self.settings.data_dir, partial, self.key, stats
                    )
                    await filesystem_call(os.replace, partial, destination)
                finally:
                    partial.unlink(missing_ok=True)
                await filesystem_call(fsync_directory, destination.parent)
                await self.archive.set_setting("last_backup_at", time.time())
                await self.archive.set_setting("backup_error", None)
                backups = sorted(
                    destination.parent.glob("*.sba"), key=lambda p: p.stat().st_mtime, reverse=True
                )
                for old in backups[self.settings.backup_keep :]:
                    old.unlink()
                return destination
            finally:
                await self._close_snapshot(path, snapshot)

    async def export(
        self,
        *,
        chat_id: int | None = None,
        include_media: bool = False,
        message_id: int | None = None,
    ) -> tuple[Path, int]:
        """Build encrypted ZIP containing JSONL + standalone HTML; bounded per message."""
        async with self.lock:
            path, snapshot = await self._snapshot()
            destination = self.settings.data_dir / "exports" / f"{uuid4().hex}.sba"
            writer = None
            try:
                writer = EncryptedWriter(destination, self.key)
                with zipfile.ZipFile(
                    _EncryptedSink(writer), "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    # JSONL lets large archives be consumed without loading them in memory.
                    with archive.open("messages.jsonl", "w", force_zip64=True) as output:
                        async for history in self._histories(snapshot, chat_id, message_id):
                            output.write((json.dumps(history, ensure_ascii=False) + "\n").encode())
                    with archive.open("index.html", "w", force_zip64=True) as output:
                        output.write(
                            b'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Telegram archive</title><style>body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}article{border-bottom:1px solid #aaa;padding:1rem 0}</style><h1>Telegram archive</h1>'
                        )
                        async for history in self._histories(snapshot, chat_id, message_id):
                            message = history["message"]
                            output.write(
                                f"<article><h2>#{message['id']} — {html.escape(str(message.get('chat_title') or message['chat_id']))}</h2>".encode()
                            )
                            if message["deleted_at"] is not None:
                                output.write(
                                    "<p>Удалено в Telegram. Сохранённые версии:</p>".encode()
                                )
                            for version in history["versions"]:
                                output.write(
                                    f"<p>{html.escape(version['kind'])} · {version['source_date']}</p><pre>{html.escape(version.get('text') or version.get('caption') or '[Структурированное сообщение: см. JSON]')}</pre>".encode()
                                )
                            output.write(b"</article>")
                        output.write(b"</html>")
                    if include_media:
                        seen = set()
                        async for history in self._histories(snapshot, chat_id, message_id):
                            for media in history["media"]:
                                if not media["object_path"] or media["object_path"] in seen:
                                    continue
                                seen.add(media["object_path"])
                                encrypted = object_path(
                                    self.settings.data_dir, media["object_path"]
                                )
                                with archive.open(
                                    f"media/{media['sha256']}.bin", "w", force_zip64=True
                                ) as output:
                                    await filesystem_call(
                                        _write_decrypted_file, output, encrypted, self.key
                                    )
                    archive.writestr(
                        "README.txt",
                        "messages.jsonl содержит полные версии и метаданные. index.html — безопасное текстовое представление. Файлы media названы по sha256; исходные имена и типы указаны в JSON.\n",
                    )
                _, size = writer.finish()
                return destination, size
            except BaseException:
                if writer is not None:
                    writer.abort()
                destination.unlink(missing_ok=True)
                raise
            finally:
                await self._close_snapshot(path, snapshot)

    async def _histories(self, snapshot: Archive, chat_id: int | None, message_id: int | None):
        if message_id is not None:
            history = await snapshot.history(message_id)
            if history:
                yield history
            return
        offset = 0
        while rows := await snapshot.list_messages(limit=100, offset=offset, chat_id=chat_id):
            for row in rows:
                if history := await snapshot.history(row["id"]):
                    yield history
            offset += len(rows)

    async def erase(self, message_id: int) -> None:
        async with self.lock:
            paths = await self.archive.erase_message(message_id)
            referenced = await self.archive.object_paths()
            for path in set(paths) - referenced:
                object_path(self.settings.data_dir, path).unlink(missing_ok=True)
            self._invalidate_exports()

    def _invalidate_exports(self) -> None:
        # Call only under self.lock: snapshots can contain erased nested context.
        for export in (self.settings.data_dir / "exports").glob("*.sba"):
            export.unlink(missing_ok=True)

    async def run(self) -> None:
        while True:
            try:
                await self._export_job()
                now = time.time()
                last_backup = await self.archive.get_setting("last_backup_at", now)
                if await self.archive.get_setting("last_backup_at") is None:
                    await self.archive.set_setting("last_backup_at", 0)
                    last_backup = 0
                if (
                    self.settings.backup_hours
                    and now - last_backup >= self.settings.backup_hours * 3600
                ):
                    await self.backup()
                # Destructive retention is opt-in, never inherited silently from legacy .env.
                days = await self.archive.get_setting(
                    "retention_days", self.settings.retention_days
                )
                if days > 0:
                    async with self.lock:
                        erased_count, paths = await self.archive.prune(now - days * 86400)
                        if erased_count:
                            self._invalidate_exports()
                        referenced = await self.archive.object_paths()
                        for path in set(paths) - referenced:
                            object_path(self.settings.data_dir, path).unlink(missing_ok=True)
                async with self.lock:
                    for expired_export in (self.settings.data_dir / "exports").glob("*.sba"):
                        if now - expired_export.stat().st_mtime > 3600:
                            expired_export.unlink(missing_ok=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("maintenance_error type=%s", type(error).__name__)
                await self.archive.set_setting("maintenance_error", type(error).__name__)
            await asyncio.sleep(5)

    async def _export_job(self) -> None:
        job = await self.archive.claim_job("export", lease_seconds=3600)
        if job is None:
            return
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(self._keep_export_lease(job, lost_lease))
        try:
            payload = job["payload"]
            if payload.get("action") == "backup":
                await self.backup()
                if lost_lease.is_set() or not await self.archive.renew_job(
                    job["id"], job["attempts"], 3600
                ):
                    return
                await self.archive.enqueue(
                    "notification",
                    f"backup-ready:{job['id']}",
                    {"text": "Зашифрованная резервная копия создана. Ключ хранится отдельно."},
                )
            else:
                path, size = await self.export(
                    chat_id=payload.get("chat_id"),
                    include_media=payload.get("include_media", False),
                    message_id=payload.get("message_id"),
                )
                if lost_lease.is_set() or not await self.archive.renew_job(
                    job["id"], job["attempts"], 3600
                ):
                    return
                limit = (
                    1_900_000_000
                    if self.settings.api_url != "https://api.telegram.org"
                    else 49_000_000
                )
                if size > limit:
                    await self.archive.enqueue(
                        "notification",
                        f"export-large:{job['id']}",
                        {
                            "text": "Экспорт подготовлен, но превышает лимит отправки Telegram. Выберите один чат или воспользуйтесь локальной командой export. Экспорт в хранилище зашифрован."
                        },
                    )
                else:
                    await self.archive.enqueue(
                        "notification",
                        f"export-ready:{job['id']}",
                        {
                            "export_path": str(path.relative_to(self.settings.data_dir)),
                            "filename": "telegram-archive.zip",
                        },
                    )
            if not lost_lease.is_set() and await self.archive.renew_job(
                job["id"], job["attempts"], 3600
            ):
                await self.archive.finish_job(job["id"], attempts=job["attempts"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not lost_lease.is_set() and await self.archive.renew_job(
                job["id"], job["attempts"], 3600
            ):
                await self.archive.retry_job(
                    job["id"], type(error).__name__, 60, attempts=job["attempts"]
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _keep_export_lease(self, job: dict, lost_lease: asyncio.Event) -> None:
        try:
            while True:
                await asyncio.sleep(_EXPORT_HEARTBEAT_SECONDS)
                if not await self.archive.renew_job(job["id"], job["attempts"], 3600):
                    lost_lease.set()
                    return
        except Exception as error:
            lost_lease.set()
            logger.error("export_lease_error type=%s", type(error).__name__)
