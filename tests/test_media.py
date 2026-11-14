import asyncio
import os
from dataclasses import replace

import pytest

from spybot.archive import Archive
from spybot.config import Settings
from spybot.crypto import decrypt_chunks
from spybot.media import MediaWorker, object_path
from spybot.telegram import TelegramError


@pytest.fixture
async def fixture(tmp_path):
    settings = Settings("1:synthetic", 1, tmp_path / "data", tmp_path / "key", min_free_bytes=0)
    settings.prepare_data_dir()
    key = os.urandom(32)
    archive = await Archive.open(settings.data_dir / "archive.db", key, 1)
    await archive.remember_connection({"id": "c", "user": {"id": 1}, "is_enabled": True})
    update = {
        "update_id": 1,
        "business_message": {
            "business_connection_id": "c",
            "chat": {"id": 2},
            "message_id": 7,
            "date": 100,
            "document": {"file_id": "file", "file_name": "private.txt", "file_size": 7},
        },
    }
    await archive.ingest_batch([update])
    await archive.apply_update(update)
    yield archive, settings, key
    await archive.close()


class API:
    async def call(self, *args, **kwargs):
        return {"file_size": 7, "file_path": "documents/file"}

    async def download(self, path):
        yield b"content"


async def test_download_survives_delete_and_is_encrypted(fixture):
    archive, settings, key = fixture
    deleted = {
        "update_id": 2,
        "deleted_business_messages": {
            "business_connection_id": "c",
            "chat": {"id": 2},
            "message_ids": [7],
        },
    }
    await archive.ingest_batch([deleted])
    await archive.apply_update(deleted)
    assert await MediaWorker(archive, API(), settings, key, asyncio.Lock()).run_once()
    media = await archive.media(1)
    assert media["state"] == "saved"
    path = object_path(settings.data_dir, media["object_path"])
    assert b"content" not in path.read_bytes()
    assert b"".join(decrypt_chunks(path, key)) == b"content"
    assert (await archive.history(1))["message"]["deleted_at"] is not None


async def test_size_limit_preserves_record_and_allows_explicit_retry(fixture):
    archive, settings, key = fixture
    small = replace(settings, max_file_bytes=5)
    await MediaWorker(archive, API(), small, key, asyncio.Lock()).run_once()
    assert (await archive.media(1))["state"] == "too_large"
    assert (await archive.stats())["messages"] == 1
    assert (await archive.retry_failed())["jobs"] == 1
    await MediaWorker(archive, API(), settings, key, asyncio.Lock()).run_once()
    assert (await archive.media(1))["state"] == "saved"


async def test_old_worker_cannot_overwrite_new_saved_result(fixture):
    archive, settings, key = fixture
    lock = asyncio.Lock()

    class SlowAPI(API):
        async def download(self, path):
            # Simulate suspension past lease, another worker completing, then stale failure.
            async with archive._transaction():
                await archive._db.execute("UPDATE jobs SET lease_until=0 WHERE kind='media'")
            await MediaWorker(archive, API(), settings, key, lock).run_once()
            raise TelegramError(400)
            yield b""

    await MediaWorker(archive, SlowAPI(), settings, key, lock).run_once()
    assert (await archive.media(1))["state"] == "saved"
    assert await archive.claim_job("media") is None


async def test_erase_during_download_cannot_resurrect(fixture):
    archive, settings, key = fixture

    class EraseAPI(API):
        async def download(self, path):
            await archive.erase_message(1)
            yield b"content"

    await MediaWorker(archive, EraseAPI(), settings, key, asyncio.Lock()).run_once()
    assert await archive.media(1) is None
    assert not list((settings.data_dir / "objects").rglob("*.enc"))
    assert not list((settings.data_dir / "staging").iterdir())


async def test_missing_file_eventually_stops_retry_but_metadata_remains(fixture):
    archive, settings, key = fixture
    async with archive._transaction():
        await archive._db.execute("UPDATE jobs SET attempts=9 WHERE kind='media'")

    class MissingAPI(API):
        async def call(self, *args, **kwargs):
            raise TelegramError(404)

    await MediaWorker(archive, MissingAPI(), settings, key, asyncio.Lock()).run_once()
    assert (await archive.media(1))["state"] == "unavailable"
    assert await archive.claim_job("media") is None
    assert (await archive.history(1))["media"][0]["file_id"] == "file"
