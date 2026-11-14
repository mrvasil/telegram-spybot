import asyncio
import json
import sqlite3
import time

import pytest
import pytest_asyncio
import sqlcipher3

from spybot.archive import Archive, ForeignConnection, IdentityMismatch, UnknownConnection

KEY = b"local-test-key-is-not-a-secret!!"
assert len(KEY) == 32


def connection(id="c", owner=99):
    return {"id": id, "user": {"id": owner}, "is_enabled": True, "date": 1}


def message(
    update_id,
    *,
    message_id=1,
    chat_id=10,
    connection_id="c",
    text="original",
    date=100,
    edit=False,
    **extra,
):
    raw = {
        "business_connection_id": connection_id,
        "message_id": message_id,
        "chat": {"id": chat_id, "title": "Chat"},
        "date": date,
        "from": None,
        "text": text,
        **extra,
    }
    return {"update_id": update_id, "edited_business_message" if edit else "business_message": raw}


def deleted(update_id, *, message_ids=None, chat_id=10):
    return {
        "update_id": update_id,
        "deleted_business_messages": {
            "business_connection_id": "c",
            "chat": {"id": chat_id},
            "message_ids": message_ids or [1],
        },
    }


@pytest_asyncio.fixture
async def archive(tmp_path):
    archive = await Archive.open(tmp_path / "archive.db", KEY, 99)
    await archive.remember_connection(connection())
    yield archive
    await archive.close()


async def apply(archive, *updates):
    await archive.ingest_batch(list(updates))
    for update in updates:
        await archive.apply_update(update)


async def test_lossless_intake_replay_and_monotonic_offset(archive):
    update = message(
        100,
        text="Текст " * 30000,
        caption="caption",
        entities=[{"type": "bold", "offset": 0, "length": 3}],
        future={"unknown": [1, {"field": "value"}]},
    )
    assert await archive.ingest_batch([update]) == 101
    pending = await archive.pending_updates()
    assert pending[0]["payload"] == update
    await archive.apply_update(update)
    await archive.apply_update(update)
    await archive.ingest_batch([message(2)])
    assert await archive.get_offset() == 101
    history = await archive.history(1)
    assert len(history["versions"]) == 1
    assert history["versions"][0]["payload"] == update["business_message"]
    assert history["versions"][0]["caption"] == "caption"


async def test_batch_failure_rolls_back_data_and_offset(archive):
    await archive.ingest_batch([message(1)])
    with pytest.raises(KeyError):
        await archive.ingest_batch([message(2), {"invalid": "no id"}])
    assert await archive.get_offset() == 2
    assert [x["update_id"] for x in await archive.pending_updates()] == [1]


async def test_unknown_and_foreign_connections(archive):
    unknown = message(1, connection_id="unknown")
    await archive.ingest_batch([unknown])
    with pytest.raises(UnknownConnection) as caught:
        await archive.apply_update(unknown)
    assert caught.value.connection_id == "unknown"
    assert (await archive.stats())["inbox_pending"] == 1
    with pytest.raises(ForeignConnection):
        await archive.remember_connection(connection("unknown", owner=12))
    await archive.apply_update(unknown)
    assert (await archive.stats())["messages"] == 0
    assert (await archive.stats())["inbox_pending"] == 0
    await apply(archive, {"update_id": 2, "business_connection": connection("foreign", owner=12)})
    assert await archive.connection("foreign") is None
    await archive.remember_connection(connection("known"))
    await apply(archive, message(3, connection_id="known"))
    assert (await archive.stats())["messages"] == 1


async def test_keys_isolate_chats_and_connections(archive):
    await archive.remember_connection(connection("second"))
    await apply(archive, message(1), message(2, chat_id=11), message(3, connection_id="second"))
    assert (await archive.stats())["messages"] == 3
    assert len(await archive.list_chats()) == 3


async def test_edit_delete_before_original_is_sticky_and_searchable(archive):
    await apply(
        archive,
        message(20, edit=True, text="new version", edit_date=300),
        deleted(21),
        message(19, text="ancient vocabulary"),
    )
    row = (await archive.list_messages())[0]
    assert row["text"] == "new version"
    assert row["deleted_at"] is not None
    history = await archive.history(row["id"])
    assert [v["text"] for v in history["versions"]] == ["ancient vocabulary", "new version"]
    assert len(history["deletion_events"]) == 1
    assert (await archive.search("ancient"))[0]["message_id"] == row["id"]
    assert (await archive.list_messages(deleted_only=True))[0]["id"] == row["id"]


async def test_delete_unknown_creates_honest_tombstone(archive):
    await apply(archive, deleted(1))
    row = (await archive.list_messages())[0]
    assert row["text"] is None and row["latest_version_id"] is None
    assert (await archive.history(row["id"]))["versions"] == []
    await apply(archive, message(2))
    assert (await archive.list_messages())[0]["deleted_at"] is not None


async def test_muted_chat_retains_every_identical_edit(archive):
    await archive.set_setting("notify_edits", False, chat_id=10)
    await apply(archive, message(1), message(2, edit=True), message(3, edit=True))
    assert (await archive.stats())["versions"] == 3
    assert await archive.claim_job("notification") is None
    assert await archive.get_setting("notify_edits", True, chat_id=11) is True


async def test_nested_media_and_context_never_replace_stronger_version(archive):
    await apply(archive, message(1, message_id=7, text="real", date=100))
    nested = {
        "message_id": 7,
        "chat": {"id": 10},
        "date": 900,
        "text": "context",
        "photo": [{"file_id": "s"}, {"file_id": "l"}],
    }
    await apply(
        archive,
        message(
            2,
            reply_to_message=nested,
            future_media={"file_id": "unknown", "thumbnail": {"file_id": "thumb"}},
        ),
    )
    stats = await archive.stats()
    assert stats["messages"] == 2 and stats["versions"] == 3 and stats["media"] == 4
    row = next(m for m in await archive.list_messages() if m["message_id"] == 7)
    assert row["text"] == "real"
    assert len((await archive.history(row["id"]))["versions"]) == 2
    jobs = [await archive.claim_job("media") for _ in range(4)]
    assert {j["payload"]["file_id"] for j in jobs} == {"s", "l", "unknown", "thumb"}


async def test_restart_retains_offset_pending_media_and_inbox(tmp_path):
    path = tmp_path / "restart.db"
    archive = await Archive.open(path, KEY, 99)
    await archive.remember_connection(connection())
    await apply(archive, message(1, document={"file_id": "file", "file_unique_id": "unique"}))
    await archive.ingest_batch([message(2)])
    await archive.close()
    archive = await Archive.open(path, KEY, 99)
    try:
        assert await archive.get_offset() == 3
        assert (await archive.pending_updates())[0]["update_id"] == 2
        assert (await archive.claim_job("media"))["payload"]["file_id"] == "file"
    finally:
        await archive.close()


async def test_actual_sqlcipher_encryption_identity_and_backup(archive, tmp_path):
    await apply(archive, message(1, text="UNIQUE_PLAINTEXT_SENTINEL"))
    await archive.bind_identity(456)
    with pytest.raises(IdentityMismatch):
        await archive.bind_identity(457)
    backup = tmp_path / "snapshot.db"
    await archive.backup_to(backup)
    assert not backup.read_bytes().startswith(b"SQLite format 3")
    assert b"UNIQUE_PLAINTEXT_SENTINEL" not in backup.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        with sqlite3.connect(backup) as plain:
            plain.execute("SELECT * FROM sqlite_master").fetchall()
    with pytest.raises(sqlcipher3.DatabaseError):
        await Archive.open(backup, b"X" * 32, 99)
    with pytest.raises(IdentityMismatch):
        await Archive.open(backup, KEY, 98)
    restored = await Archive.open(backup, KEY, 99)
    try:
        assert (await restored.search("UNIQUE_PLAINTEXT_SENTINEL"))[0]["message_id"] == 1
        await restored.bind_identity(456)
    finally:
        await restored.close()
    with pytest.raises(ValueError):
        await archive.backup_to(backup)


async def test_erase_removes_content_jobs_raw_and_prevents_resurrection(archive):
    first = message(1, text="secretword", photo=[{"file_id": "file"}])
    await apply(archive, first)
    media = (await archive.message_media(1))[0]
    assert await archive.update_media(media["id"], state="saved", object_path="objects/abc")
    await apply(
        archive,
        message(2, message_id=2, text="unrelated", reply_to_message=first["business_message"]),
    )
    assert await archive.erase_message(1) == ["objects/abc"]
    assert await archive.history(1) is None
    assert await archive.search("secretword") == []
    assert await archive.object_paths() == set()
    assert not await archive.update_media(media["id"], state="saved", object_path="objects/race")
    remaining = await archive.history(2)
    assert "secretword" not in json.dumps(remaining)
    await apply(archive, first, message(3, text="secretword", edit=True))
    assert (await archive.stats())["messages"] == 1
    assert await archive.search("secretword") == []
    # Verify erasure does not leave reintroduced content hidden in the raw inbox.
    async with archive._lock:
        rows = await archive._all("SELECT payload FROM inbox")
        assert all("secretword" not in r["payload"] for r in rows)


async def test_queue_retry_lease_recovery_and_notification_uncertainty(archive, monkeypatch):
    now = time.time()
    monkeypatch.setattr("spybot.archive.time.time", lambda: now)
    id = await archive.enqueue("media", "unique", {"media_id": 42, "file_id": "f"})
    assert await archive.enqueue("media", "unique", {"different": "ignored"}) == id
    job = await archive.claim_job("media", lease_seconds=10)
    assert job["attempts"] == 1
    assert await archive.claim_job("media") is None
    now += 11
    job = await archive.claim_job("media")
    assert job["id"] == id and job["attempts"] == 2
    await archive.retry_job(id, "network_error", 20)
    assert await archive.claim_job("media") is None
    now += 21
    assert (await archive.claim_job("media"))["attempts"] == 3
    await archive.finish_job(id)
    assert await archive.claim_job("media") is None
    await archive.enqueue("notification", "notice", {"message_id": 1})
    await archive.claim_job("notification", lease_seconds=10)
    now += 11
    assert await archive.claim_job("notification") is None


async def test_failed_update_backoff_does_not_hotloop(archive):
    await archive.ingest_batch([message(1)])
    await archive.fail_update(1, "retry_later", time.time() + 100)
    assert await archive.pending_updates() == []
    await archive.fail_update(1, "do not store raw message here", 0)
    assert (await archive.pending_updates())[0]["attempts"] == 2


async def test_application_failure_rolls_back_all_side_effects(archive, monkeypatch):
    update = message(1, photo=[{"file_id": "f"}])
    await archive.ingest_batch([update])
    original = archive._enqueue

    async def fail(*args):
        await original(*args)
        raise RuntimeError("synthetic_failure")

    monkeypatch.setattr(archive, "_enqueue", fail)
    with pytest.raises(RuntimeError):
        await archive.apply_update(update)
    assert (await archive.stats()) == {
        "messages": 0,
        "versions": 0,
        "deleted": 0,
        "media": 0,
        "pending_media": 0,
        "inbox_pending": 1,
        "jobs_pending": 0,
        "jobs_attention": 0,
        "jobs_uncertain": 0,
        "media_attention": 0,
        "last_received_at": pytest.approx(time.time(), abs=5),
    }
    monkeypatch.setattr(archive, "_enqueue", original)
    await archive.apply_update(update)
    assert (await archive.stats())["media"] == 1


async def test_cancelled_transaction_rolls_back_and_connection_remains_usable(archive, monkeypatch):
    update = message(1, photo=[{"file_id": "f"}])
    await archive.ingest_batch([update])
    original = archive._enqueue
    entered = asyncio.Event()

    async def pause(*args):
        await original(*args)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(archive, "_enqueue", pause)
    task = asyncio.create_task(archive.apply_update(update))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await archive.stats())["messages"] == 0
    monkeypatch.setattr(archive, "_enqueue", original)
    await archive.apply_update(update)
    assert (await archive.stats())["versions"] == 1


async def test_search_operator_input_is_literal_and_safe(archive):
    await apply(archive, message(1, text="hello OR something"))
    assert await archive.search("OR")
    assert await archive.search('" NEAR(') == []
    assert await archive.search("*") == []
    assert await archive.search("") == []


async def test_prune_is_explicit_and_sticky(archive):
    await apply(archive, message(1, date=10), message(2, message_id=2, date=100))
    await archive.prune(50)
    assert len(await archive.list_messages()) == 1
    await apply(archive, message(3, date=10))
    assert len(await archive.list_messages()) == 1


async def test_import_can_apply_silently_without_changing_poll_offset(archive):
    await archive.apply_update(
        message(-1, edit=True, legacy_import={"source": "test"}), notify=False
    )
    assert await archive.get_offset() is None
    assert await archive.claim_job("notification") is None
    assert (await archive.history(1))["versions"][0]["payload"]["legacy_import"] == {
        "source": "test"
    }


async def test_known_connection_identity_is_immutable(archive):
    with pytest.raises(IdentityMismatch):
        await archive.remember_connection(connection(owner=123))
    await apply(archive, message(1))
    assert (await archive.stats())["messages"] == 1


async def test_disabled_connection_keeps_inflight_and_original_cannot_revert_edit(archive):
    raw = connection()
    raw["is_enabled"] = False
    await archive.remember_connection(raw)
    await apply(
        archive, message(1, edit=True, text="edit", date=100), message(2, text="original", date=100)
    )
    assert (await archive.list_messages())[0]["text"] == "edit"


async def test_history_exports_original_update_envelope(archive):
    update = message(1)
    update["future_top_level"] = {"data": ["all", "preserved"]}
    await apply(archive, update)
    assert (await archive.history(1))["updates"] == [{"update_id": 1, "payload": update}]


async def test_reads_wait_for_uncommitted_application(archive, monkeypatch):
    update = message(1, photo=[{"file_id": "f"}])
    await archive.ingest_batch([update])
    original = archive._enqueue
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def pause(*args):
        await original(*args)
        entered.set()
        await proceed.wait()

    monkeypatch.setattr(archive, "_enqueue", pause)
    application = asyncio.create_task(archive.apply_update(update))
    await entered.wait()
    reading = asyncio.create_task(archive.stats())
    await asyncio.sleep(0)
    assert not reading.done()
    proceed.set()
    await application
    assert (await reading)["versions"] == 1


async def test_sqlcipher_runtime_pragmas_are_enforced(archive):
    async with archive._lock:
        assert (await archive._one("PRAGMA journal_mode"))["journal_mode"] == "wal"
        assert (await archive._one("PRAGMA synchronous"))["synchronous"] == 2
        assert (await archive._one("PRAGMA temp_store"))["temp_store"] == 2
        assert (await archive._one("PRAGMA foreign_keys"))["foreign_keys"] == 1
        assert (await archive._one("PRAGMA cipher_version"))["cipher_version"]


async def test_erase_preserves_unrelated_pending_reply_and_mixed_deletion(archive):
    erased = message(1, text="erase this")
    await apply(archive, erased, message(2, message_id=2, text="keep me"))
    reply = message(
        3, message_id=3, text="pending reply", reply_to_message=erased["business_message"]
    )
    await archive.ingest_batch([reply, deleted(4, message_ids=[1, 2])])
    await archive.erase_message(1)
    pending = await archive.pending_updates()
    assert [r["update_id"] for r in pending] == [3, 4]
    assert pending[1]["payload"]["deleted_business_messages"]["message_ids"] == [2]
    assert "erase this" not in json.dumps(pending)
    for row in pending:
        await archive.apply_update(row["payload"])
    rows = await archive.list_messages()
    assert next(row for row in rows if row["message_id"] == 3)["text"] == "pending reply"
    assert next(row for row in rows if row["message_id"] == 2)["deleted_at"] is not None
    assert await archive.get_offset() == 5


async def test_erase_redacts_deep_context_in_every_surviving_version(archive):
    c = {
        "message_id": 3,
        "chat": {"id": 10},
        "date": 100,
        "text": "private nested sentinel",
        "photo": [{"file_id": "private_file"}],
    }
    b = {
        "message_id": 2,
        "chat": {"id": 10},
        "date": 100,
        "text": "retained B",
        "reply_to_message": c,
    }
    await apply(archive, message(1, text="retained A", reply_to_message=b))
    c_id = next(row["id"] for row in await archive.list_messages() if row["message_id"] == 3)
    await archive.erase_message(c_id)
    assert await archive.search("private") == []
    for row in await archive.list_messages():
        serialized = json.dumps(await archive.history(row["id"]))
        assert "private nested sentinel" not in serialized
        assert "private_file" not in serialized
    assert (await archive.stats())["messages"] == 2


async def test_job_heartbeat_and_stale_attempt_fencing(archive, monkeypatch):
    now = time.time()
    monkeypatch.setattr("spybot.archive.time.time", lambda: now)
    id = await archive.enqueue("media", "lease", {"media_id": 1})
    first = await archive.claim_job("media", lease_seconds=10)
    now += 5
    assert await archive.renew_job(id, first["attempts"], lease_seconds=20)
    now += 6
    assert await archive.claim_job("media") is None
    now += 20
    assert not await archive.renew_job(id, first["attempts"], lease_seconds=20)
    second = await archive.claim_job("media")
    assert second["attempts"] == first["attempts"] + 1
    assert not await archive.finish_job(id, attempts=first["attempts"])
    assert not await archive.retry_job(id, "stale_error", 0, attempts=first["attempts"])
    assert not await archive.renew_job(id, first["attempts"], lease_seconds=20)
    assert await archive.finish_job(id, attempts=second["attempts"])
    assert not await archive.retry_job(id, "already_done", 0, attempts=second["attempts"])


async def test_owner_reply_recovers_previously_unseen_file(archive):
    raw = message(1, message_id=20, text="owner reply")
    raw["business_message"]["from"] = {"id": 99}
    raw["business_message"]["reply_to_message"] = {
        "message_id": 19,
        "date": 1,
        "chat": {"id": 10},
        "voice": {"file_id": "previously-unseen", "file_unique_id": "voice-one"},
        "has_protected_content": True,
    }
    await archive.ingest_batch([raw])
    await archive.apply_update(raw)
    records = await archive.list_messages()
    recovered = next(row for row in records if row["message_id"] == 19)
    history = await archive.history(recovered["id"])
    assert history["versions"][0]["kind"] == "context"
    assert history["versions"][0]["payload"]["has_protected_content"] is True
    assert history["media"][0]["file_id"] == "previously-unseen"
