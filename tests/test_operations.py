import asyncio
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
                {"format": 1, "files": {"archive.db": {"bytes": 1}, "../outside": {"bytes": 1}}}
            ),
        )
        archive.writestr("archive.db", "x")
        archive.writestr("../outside", "x")
    path = tmp_path / "bad"
    writer = EncryptedWriter(path, key)
    writer.write(payload.getvalue())
    writer.finish()
    with pytest.raises((ValueError, KeyError)):
        restore_backup(path, tmp_path / "destination", key)
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "destination").exists()
