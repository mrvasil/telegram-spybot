import asyncio
import hashlib
import io
import json
import os
import zipfile

import pytest

from spybot.archive import Archive
from spybot.config import Settings
from spybot.crypto import EncryptedReader, EncryptedWriter, decrypt_chunks
from spybot.operations import Operations, restore_backup


@pytest.fixture
async def environment(tmp_path):
    settings = Settings("1:synthetic", 1, tmp_path / "data", tmp_path / "key")
    settings.prepare_data_dir()
    key = os.urandom(32)
    archive = await Archive.open(settings.data_dir / "archive.db", key, 1)
    await archive.remember_connection({"id": "c", "user": {"id": 1}, "is_enabled": True})
    update = {
        "update_id": 1,
        "business_message": {
            "business_connection_id": "c",
            "chat": {"id": 2, "first_name": "<name>"},
            "message_id": 7,
            "date": 100,
            "text": "<script>SECRET</script>",
        },
    }
    await archive.ingest_batch([update])
    await archive.apply_update(update)
    yield archive, settings, key, Operations(archive, settings, key, asyncio.Lock())
    await archive.close()


async def test_backup_restores_encrypted_archive(environment, tmp_path):
    archive, settings, key, operations = environment
    backup = await operations.backup()
    assert b"SECRET" not in backup.read_bytes()
    target = tmp_path / "restored"
    manifest = restore_backup(backup, target, key)
    restored = await Archive.open(target / "archive.db", key, 1)
    try:
        assert (await restored.stats())["messages"] == manifest["stats"]["messages"] == 1
        assert await restored.get_offset() == 2
        assert (await restored.search("SECRET"))[0]["message_id"] == 1
    finally:
        await restored.close()
    with pytest.raises(ValueError):
        restore_backup(backup, target, key)


async def test_export_is_lossless_and_html_is_escaped(environment):
    _, _, key, operations = environment
    path, size = await operations.export()
    assert b"SECRET" not in path.read_bytes()
    with EncryptedReader(path, key) as reader, zipfile.ZipFile(reader) as export:
        record = json.loads(export.read("messages.jsonl"))
        assert record["versions"][0]["payload"]["text"] == "<script>SECRET</script>"
        html = export.read("index.html")
        assert b"<script>" not in html
        assert b"&lt;script&gt;" in html
        assert record["updates"][0]["payload"]["update_id"] == 1
    assert size == len(b"".join(decrypt_chunks(path, key)))


def test_encrypted_reader_random_access(tmp_path):
    key = os.urandom(32)
    path = tmp_path / "file"
    content = os.urandom(700000)
    writer = EncryptedWriter(path, key)
    writer.write(content)
    writer.finish()
    with EncryptedReader(path, key) as reader:
        for offset in (270000, 0, 699999, 524280):
            reader.seek(offset)
            assert reader.read(30) == content[offset : offset + 30]
        reader.seek(-20, 2)
        assert reader.read() == content[-20:]


def test_restore_rejects_traversal_before_overwriting(tmp_path):
    key = os.urandom(32)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": 1,
                    "files": {
                        name: {"bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
                        for name in ("archive.db", "objects/../../outside")
                    },
                }
            ),
        )
        archive.writestr("archive.db", "x")
        archive.writestr("objects/../../outside", "x")
    path = tmp_path / "bad"
    writer = EncryptedWriter(path, key)
    writer.write(payload.getvalue())
    writer.finish()
    with pytest.raises(ValueError, match="Unsafe backup path"):
        restore_backup(path, tmp_path / "destination", key)
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "destination").exists()


async def test_cancel_snapshot_waits_for_backup_and_removes_sidecars(environment, monkeypatch):
    archive, settings, _, operations = environment
    created = asyncio.Event()
    release = asyncio.Event()
    original = archive.backup_to

    async def paused_backup(path):
        await original(path)
        for suffix in ("-wal", "-shm"):
            type(path)(str(path) + suffix).touch()
        created.set()
        await release.wait()

    monkeypatch.setattr(archive, "backup_to", paused_backup)
    task = asyncio.create_task(operations.backup())
    await created.wait()
    task.cancel()
    await asyncio.sleep(0)
    waiting = not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiting, "cleanup must await outstanding native snapshot work"
    assert list((settings.data_dir / "staging").iterdir()) == []


async def test_export_constructor_failure_closes_snapshot(environment, monkeypatch):
    _, settings, _, operations = environment

    def fail_writer(*_):
        raise OSError("synthetic constructor failure")

    monkeypatch.setattr("spybot.operations.EncryptedWriter", fail_writer)
    with pytest.raises(OSError, match="constructor"):
        await operations.export()
    assert list((settings.data_dir / "staging").iterdir()) == []


async def test_export_job_renews_lease_while_working(environment, monkeypatch):
    archive, settings, _, operations = environment
    renewed = asyncio.Event()
    original = archive.renew_job
    renewals = []

    async def renew(id, attempts, lease_seconds):
        renewals.append((id, attempts, lease_seconds))
        result = await original(id, attempts, lease_seconds)
        renewed.set()
        return result

    async def waiting_export(**_):
        await asyncio.wait_for(renewed.wait(), 1)
        return settings.data_dir / "exports" / "synthetic.sba", 10

    monkeypatch.setattr(archive, "renew_job", renew)
    monkeypatch.setattr(operations, "export", waiting_export)
    monkeypatch.setattr("spybot.operations._EXPORT_HEARTBEAT_SECONDS", 0.001)
    job_id = await archive.enqueue("export", "synthetic-export", {})
    await operations._export_job()
    assert len(renewals) >= 3
    assert all(row == (job_id, 1, 3600) for row in renewals)
    notification = await archive.claim_job("notification")
    assert notification["payload"]["export_path"] == "exports/synthetic.sba"
    assert await archive.claim_job("export") is None


async def test_export_job_does_not_publish_or_revive_expired_lease(environment, monkeypatch):
    archive, settings, _, operations = environment
    job_id = await archive.enqueue("export", "expired-export", {})

    async def expired_export(**_):
        async with archive._transaction():
            await archive._db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
        return settings.data_dir / "exports" / "synthetic.sba", 10

    monkeypatch.setattr(operations, "export", expired_export)
    await operations._export_job()
    assert await archive.claim_job("notification") is None
    reclaimed = await archive.claim_job("export")
    assert reclaimed["id"] == job_id and reclaimed["attempts"] == 2
