import os
import sqlite3

import pytest

from spybot.archive import Archive
from spybot.config import Settings
from spybot.crypto import decrypt_chunks
from spybot.legacy import import_legacy
from spybot.media import object_path


@pytest.fixture
async def legacy(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    (root / "media").mkdir()
    (root / "media" / "file").write_bytes(b"legacy media")
    source = root / "messages.db"
    db = sqlite3.connect(source)
    db.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
    CREATE TABLE messages(chat_id INTEGER, message_id INTEGER, user_id INTEGER,text TEXT,date TEXT);
    CREATE TABLE message_actions(id INTEGER PRIMARY KEY, chat_id INTEGER,message_id INTEGER,action_type TEXT,old_text TEXT,new_text TEXT,action_date TEXT);
    CREATE TABLE media_files(id INTEGER PRIMARY KEY,chat_id INTEGER,message_id INTEGER,file_id TEXT,media_type TEXT,media_path TEXT);
    CREATE TABLE settings(key TEXT PRIMARY KEY,value INTEGER);
    INSERT INTO users VALUES(3,'test');
    INSERT INTO messages VALUES(3,7,3,'new','2025-01-01T00:00:00+00:00');
    INSERT INTO message_actions VALUES(1,3,7,'edit','old','new','2025-01-01T01:00:00');
    INSERT INTO message_actions VALUES(2,3,7,'delete','new',NULL,'2025-01-01T02:00:00');
    INSERT INTO media_files VALUES(1,3,7,'legacy-file','document','media/file');
    INSERT INTO settings VALUES('notify_edited',0);
    """)
    db.commit()
    db.close()
    settings = Settings("1:synthetic", 1, tmp_path / "new", tmp_path / "key", min_free_bytes=0)
    settings.prepare_data_dir()
    key = os.urandom(32)
    archive = await Archive.open(settings.data_dir / "archive.db", key, 1)
    yield archive, settings, key, source, root
    await archive.close()


async def test_import_is_readonly_repeatable_and_preserves_offset(legacy):
    archive, settings, key, source, root = legacy
    before = source.read_bytes()
    dry = await import_legacy(archive, settings, key, source, root, None)
    assert dry["messages"] == 1 and dry["available_files"] == 1 and dry["naive_dates"] == 2
    assert (await archive.stats())["messages"] == 0
    with pytest.raises(ValueError, match="source-timezone"):
        await import_legacy(archive, settings, key, source, root, None, apply=True)
    await archive.ingest_batch([{"update_id": 100, "message": {"text": "control"}}])
    await import_legacy(archive, settings, key, source, root, "UTC", apply=True)
    await import_legacy(archive, settings, key, source, root, "UTC", apply=True)
    assert source.read_bytes() == before
    assert await archive.get_offset() == 101
    stats = await archive.stats()
    assert stats["messages"] == 1 and stats["versions"] == 2
    history = await archive.history(1)
    assert history["versions"][0]["text"] == "old"
    assert history["versions"][1]["text"] == "new"
    assert history["message"]["deleted_at"] is not None
    assert await archive.claim_job("notification") is None
    media = history["media"][0]
    assert media["state"] == "saved"
    assert (
        b"".join(decrypt_chunks(object_path(settings.data_dir, media["object_path"]), key))
        == b"legacy media"
    )
    assert await archive.get_setting("notify_edits") is False


async def test_import_rejects_external_media_path(legacy, tmp_path):
    archive, settings, key, source, root = legacy
    external = tmp_path / "outside"
    external.write_bytes(b"not authorized")
    db = sqlite3.connect(source)
    db.execute("UPDATE media_files SET media_path=?", (str(external),))
    db.commit()
    db.close()
    report = await import_legacy(archive, settings, key, source, root, "UTC")
    assert report["unsafe_paths"] == 1 and report["available_files"] == 0
