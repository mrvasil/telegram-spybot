"""Independent, retryable encrypted media acquisition and authenticated streaming."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from spybot.async_utils import filesystem_call
from spybot.config import Settings
from spybot.crypto import EncryptedWriter, decrypt_chunks, fsync_directory, verify_object
from spybot.telegram import TelegramAPI, TelegramError


class MediaLimitError(Exception):
    pass


class DiskSpaceError(Exception):
    pass


def object_path(data_dir: Path, relative: str) -> Path:
    root = (data_dir / "objects").resolve()
    path = (data_dir / relative).resolve()
    if root not in path.parents:
        raise ValueError("Invalid object path")
    return path


def _next_chunk(iterator: Any) -> bytes | None:
    return next(iterator, None)


async def decrypted_stream(path: Path, key: bytes) -> AsyncIterator[bytes]:
    iterator = decrypt_chunks(path, key)
    try:
        while True:
            chunk = await filesystem_call(_next_chunk, iterator)
            if chunk is None:
                break
            yield chunk
    finally:
        iterator.close()


class MediaWorker:
    def __init__(
        self,
        archive: Any,
        api: TelegramAPI,
        settings: Settings,
        key: bytes,
        maintenance_lock: asyncio.Lock,
    ):
        self.archive = archive
        self.api = api
        self.settings = settings
        self.key = key
        self.maintenance_lock = maintenance_lock

    async def run_once(self) -> bool:
        job = await self.archive.claim_job("media", lease_seconds=3600)
        if job is None:
            return False
        media = await self.archive.media(job["payload"]["media_id"])
        if media is None:
            await self.archive.finish_job(job["id"], attempts=job["attempts"])
            return True
        staging = self.settings.data_dir / "staging" / f"{uuid4().hex}.part"
        writer: EncryptedWriter | None = None
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(60)
                if not await self.archive.renew_job(job["id"], job["attempts"], 3600):
                    lease_lost.set()
                    return

        pulse = asyncio.create_task(heartbeat())
        try:
            if shutil.disk_usage(self.settings.data_dir).free < self.settings.min_free_bytes:
                raise DiskSpaceError
            info = await self.api.call("getFile", file_id=media["file_id"])
            size = info.get("file_size")
            if size is not None and size > self.settings.max_file_bytes:
                raise MediaLimitError
            if not info.get("file_path"):
                raise TelegramError(404, category="file_path_missing")
            writer = await filesystem_call(EncryptedWriter, staging, self.key)
            async for chunk in self._download(info["file_path"]):
                if lease_lost.is_set():
                    raise RuntimeError("media_lease_lost")
                if writer.size + len(chunk) > self.settings.max_file_bytes:
                    raise MediaLimitError
                if shutil.disk_usage(self.settings.data_dir).free < self.settings.min_free_bytes:
                    raise DiskSpaceError
                await filesystem_call(writer.write, chunk)
            digest, downloaded = await filesystem_call(writer.finish)
            if size is not None and downloaded != size:
                raise TelegramError(502, category="download_size_mismatch")
            relative = f"objects/{digest[:2]}/{digest}.enc"
            destination = object_path(self.settings.data_dir, relative)
            async with self.maintenance_lock:
                # An explicit erasure may have happened during the network download.
                if (
                    not await self.archive.renew_job(job["id"], job["attempts"], 3600)
                    or await self.archive.media(media["id"]) is None
                ):
                    staging.unlink(missing_ok=True)
                    await self.archive.finish_job(job["id"], attempts=job["attempts"])
                    return True
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if destination.exists():
                    if await filesystem_call(verify_object, destination, self.key) != (
                        digest,
                        downloaded,
                    ):
                        raise ValueError("Existing object failed integrity check")
                    staging.unlink()
                else:
                    await filesystem_call(os.replace, staging, destination)
                    await filesystem_call(fsync_directory, destination.parent)
                await self.archive.update_media(
                    media["id"],
                    state="saved",
                    object_path=relative,
                    sha256=digest,
                    size=downloaded,
                    error=None,
                )
                await self.archive.finish_job(job["id"], attempts=job["attempts"])
        except asyncio.CancelledError:
            if writer is not None:
                await filesystem_call(writer.abort)
            raise
        except (
            TelegramError,
            MediaLimitError,
            DiskSpaceError,
            OSError,
            ValueError,
            RuntimeError,
        ) as error:
            if writer is not None:
                await filesystem_call(writer.abort)
            if not await self.archive.renew_job(job["id"], job["attempts"], 3600):
                return True
            code = (
                error.category + ":" + str(error.code)
                if isinstance(error, TelegramError)
                else type(error).__name__
            )
            too_large = (
                isinstance(error, MediaLimitError)
                or isinstance(error, TelegramError)
                and error.category == "file_too_large"
            )
            terminal = (
                too_large
                or isinstance(error, TelegramError)
                and (error.code in {400, 401, 403} or error.code == 404 and job["attempts"] >= 10)
            )
            state = "too_large" if too_large else "unavailable" if terminal else "failed"
            await self.archive.update_media(media["id"], state=state, error=code)
            delay = max(
                error.retry_after if isinstance(error, TelegramError) else 0,
                min(2 ** min(job["attempts"] + 1, 10), 3600),
            )
            await self.archive.retry_job(
                job["id"], code, delay, terminal=terminal, attempts=job["attempts"]
            )
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        return True

    async def _download(self, file_path: str) -> AsyncIterator[bytes]:
        if file_path.startswith("/"):
            # Local Bot API --local returns paths, accessible only via an explicit read-only mount.
            root_value = os.getenv("LOCAL_BOT_API_FILES_DIR")
            if not root_value:
                raise TelegramError(400, category="local_files_mount_required")
            root = Path(root_value).resolve()
            path = Path(file_path).resolve()
            if root not in path.parents:
                raise TelegramError(400, category="local_files_outside_mount")
            stream = await filesystem_call(lambda: path.open("rb"))
            try:
                while chunk := await filesystem_call(stream.read, 64 * 1024):
                    yield chunk
            finally:
                await filesystem_call(stream.close)
        else:
            async for chunk in self.api.download(file_path):
                yield chunk

    async def run(self) -> None:
        while True:
            if not await self.run_once():
                await asyncio.sleep(0.3)
