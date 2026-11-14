import asyncio
import html
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from spybot.archive import Archive
from spybot.config import Settings
from spybot.crypto import EncryptedWriter
from spybot.operations import Operations
from spybot.render import edit_visible, escaped_parts, history_text
from spybot.telegram import TelegramError
from spybot.ui import Commands, NotificationWorker, quiet_until

KEY = b"a" * 32


class FakeAPI:
    def __init__(self, error=None):
        self.sent = []
        self.documents = []
        self.calls = []
        self.error = error

    async def send_text(self, owner, text, **options):
        if self.error:
            raise self.error
        self.sent.append((owner, text, options))

    async def send_document(self, owner, chunks, filename, caption):
        if self.error:
            raise self.error
        data = b"".join([chunk async for chunk in chunks])
        self.documents.append((owner, data, filename, caption))

    async def call(self, method, **parameters):
        self.calls.append((method, parameters))
        if self.error and method != "sendMessage":
            raise self.error


@pytest_asyncio.fixture
async def context(tmp_path):
    settings = Settings(
        "fake", 99, tmp_path, tmp_path.parent / "test.key", min_free_bytes=0, backup_hours=0
    )
    settings.prepare_data_dir()
    archive = await Archive.open(tmp_path / "archive.db", KEY, 99)
    await archive.remember_connection({"id": "c", "user": {"id": 99}, "is_enabled": True})
    operations = Operations(archive, settings, KEY, asyncio.Lock())
    commands = Commands(archive, settings, operations)
    yield archive, settings, operations, commands
    await archive.close()


def command(text=None, update_id=100, *, owner=99, chat=99, private=True):
    message = {
        "from": {"id": owner},
        "chat": {"id": chat, "type": "private" if private else "group"},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def callback(data, update_id=100, *, owner=99, chat=99, private=True):
    message = command(owner=owner, chat=chat, private=private)["message"]
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": owner},
            "message": message,
            "data": data,
        },
    }


async def jobs(archive, kind="notification"):
    async with archive._lock:
        return await archive._all("SELECT * FROM jobs WHERE kind=? ORDER BY id", (kind,))


async def payloads(archive):
    import json

    return [json.loads(row["payload"]) for row in await jobs(archive)]


async def store(archive, *, update_id=1, message_id=1, text="До", edit=False, **fields):
    raw = {
        "business_connection_id": "c",
        "message_id": message_id,
        "date": update_id,
        "chat": {"id": 10, "type": "private", "first_name": "<Чат>"},
        "text": text,
        **fields,
    }
    update = {
        "update_id": update_id,
        "edited_business_message" if edit else "business_message": raw,
    }
    await archive.ingest_batch([update])
    await archive.apply_update(update)
    return (await archive.list_messages(chat_id=10))[0]["id"]


async def deleted(archive, update_id=3, message_id=1):
    update = {
        "update_id": update_id,
        "deleted_business_messages": {
            "business_connection_id": "c",
            "chat": {"id": 10},
            "message_ids": [message_id],
        },
    }
    await archive.ingest_batch([update])
    await archive.apply_update(update)


def worker(context, api=None):
    archive, settings, operations, _ = context
    result = NotificationWorker(archive, api or FakeAPI(), settings, KEY, operations)

    async def no_pacing():
        pass

    result._paced = no_pacing
    return result


@pytest.mark.parametrize(
    "update",
    [
        command("/status", owner=7),
        command("/status", chat=10),
        command("/status", private=False),
        callback("set:muted:1:g", owner=7),
        callback("set:muted:1:g", chat=10),
        callback("set:muted:1:g", private=False),
        {
            "update_id": 100,
            "callback_query": {
                "id": "inline",
                "from": {"id": 99},
                "inline_message_id": "x",
                "data": "set:muted:1:g",
            },
        },
    ],
)
async def test_foreign_group_and_inline_updates_ignored(context, update):
    archive, _, _, commands = context
    await commands.handle(update)
    assert await jobs(archive) == []
    assert await archive.get_setting("muted") is None


async def test_repeated_callback_sets_desired_value_and_deduplicates_response(context):
    archive, _, _, commands = context
    update = callback("set:notify_edits:0:g")
    await commands.handle(update)
    await commands.handle(update)
    assert await archive.get_setting("notify_edits") is False
    assert len(await jobs(archive)) == 2  # acknowledgment and one response
    assert (await payloads(archive))[0] == {"answer_callback_query": "callback-100"}


async def test_mute_preserves_every_version(context):
    archive, _, _, commands = context
    await commands.handle(command("/mute 10"))
    message_id = await store(archive)
    await store(archive, update_id=2, edit=True, text="После")
    sender = worker(context)
    while await sender.run_once():
        pass
    assert len((await archive.history(message_id))["versions"]) == 2
    assert len(sender.api.sent) == 1  # command acknowledgment only


@pytest.mark.parametrize(
    "text",
    [
        None,
        "/message abc",
        "/quiet 55:00-01:00",
        "/quiet 22:00-08:00 Invalid/Zone",
        "/digest -1",
        "/ignore 101",
        "/retention nope",
        "/search_chat nope",
        "/unknown",
    ],
)
async def test_malformed_and_nontext_owner_messages_have_helpful_responses(context, text):
    archive, _, _, commands = context
    await commands.handle(command(text))
    messages = [payload["text"] for payload in await payloads(archive) if "text" in payload]
    assert messages and all(len(value) <= 3900 for value in messages)


async def test_deleted_unknown_content_and_history(context):
    archive, settings, _, commands = context
    await deleted(archive, message_id=9)
    tombstone = (await archive.list_messages())[0]["id"]
    await commands.handle(command(f"/message {tombstone}"))
    assert "Исходник недоступен" in "".join(
        payload.get("text", "") for payload in await payloads(archive)
    )
    message_id = await store(
        archive, update_id=4, text=None, future_message={"arbitrary": "<unknown>"}
    )
    rendered = history_text(await archive.history(message_id), 0, settings.timezone)
    assert "future_message" in rendered and "<unknown>" in rendered
    await commands.handle(command(f"/{message_id}", update_id=101))
    body = "".join(payload.get("text", "") for payload in await payloads(archive))
    assert "&lt;unknown&gt;" in body
    assert "кто удалил" in body


def test_long_escaped_text_is_complete_and_entities_are_not_split():
    original = "<&>😀" * 5000
    parts = escaped_parts(original)
    assert len(parts) > 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 3900 for part in parts)
    assert "".join(html.unescape(part) for part in parts) == original


async def test_all_saved_files_paginated_and_bound_to_message(context):
    archive, _, _, commands = context
    photo = [{"file_id": f"f{i}", "file_unique_id": f"u{i}"} for i in range(11)]
    message_id = await store(archive, photo=photo)
    await commands.handle(callback(f"files:{message_id}:0"))
    first = (await payloads(archive))[-1]
    callbacks = [
        item["callback_data"] for row in first["reply_markup"]["inline_keyboard"] for item in row
    ]
    assert f"files:{message_id}:1" in callbacks
    assert sum(value.startswith("media:") for value in callbacks) == 8
    await commands.handle(callback(f"files:{message_id}:1", update_id=101))
    second = (await payloads(archive))[-1]
    assert (
        sum(
            item["callback_data"].startswith("media:")
            for row in second["reply_markup"]["inline_keyboard"]
            for item in row
        )
        == 3
    )


async def test_unavailable_file_never_calls_document_send(context):
    archive, _, _, _ = context
    await store(archive, document={"file_id": "f"})
    media_id = (await archive.message_media(1))[0]["id"]
    await archive.enqueue("notification", "get-file", {"media_id": media_id})
    sender = worker(context)
    await sender.run_once()
    assert sender.api.documents == []
    assert "не сохранён" in sender.api.sent[0][1]


async def test_authenticated_file_delivery_sanitizes_basename(context):
    archive, settings, _, _ = context
    message_id = await store(
        archive, document={"file_id": "f", "file_name": "../../secret\\actual.txt"}
    )
    media_id = (await archive.message_media(message_id))[0]["id"]
    path = settings.data_dir / "objects" / "test.enc"
    writer = EncryptedWriter(path, KEY)
    writer.write(b"test content")
    digest, size = writer.finish()
    await archive.update_media(
        media_id, state="saved", object_path="objects/test.enc", sha256=digest, size=size
    )
    await archive.enqueue(
        "notification", "get-file", {"media_id": media_id, "message_id": message_id}
    )
    sender = worker(context)
    await sender.run_once()
    assert sender.api.documents[0][1:3] == (b"test content", "actual.txt")


async def test_file_integrity_failure_never_streams_to_api(context):
    archive, settings, _, _ = context
    message_id = await store(archive, document={"file_id": "f"})
    media_id = (await archive.message_media(message_id))[0]["id"]
    path = settings.data_dir / "objects" / "corrupt.enc"
    path.write_bytes(b"bad file")
    await archive.update_media(media_id, state="saved", object_path="objects/corrupt.enc")
    await archive.enqueue("notification", "get-file", {"media_id": media_id})
    sender = worker(context)
    await sender.run_once()
    assert sender.api.documents == []
    assert (await jobs(archive))[-1]["state"] == "failed"


async def test_protected_file_is_preserved_but_not_resent(context):
    archive, settings, _, _ = context
    message_id = await store(archive, document={"file_id": "protected"}, has_protected_content=True)
    media_id = (await archive.message_media(message_id))[0]["id"]
    path = settings.data_dir / "objects" / "protected.enc"
    writer = EncryptedWriter(path, KEY)
    writer.write(b"protected bytes")
    digest, size = writer.finish()
    await archive.update_media(
        media_id, state="saved", object_path="objects/protected.enc", sha256=digest, size=size
    )
    await archive.enqueue(
        "notification", "protected-file", {"media_id": media_id, "message_id": message_id}
    )
    sender = worker(context)
    await sender.run_once()
    assert sender.api.documents == []
    assert "защитой содержимого" in sender.api.sent[0][1]
    assert (await archive.media(media_id))["state"] == "saved"
    assert path.is_file()


def test_quiet_hours_use_zoneinfo_and_cross_midnight():
    zone = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 11, 14, 23, 30, tzinfo=zone).timestamp()
    quiet = {"start": "23:00", "end": "08:00", "timezone": "Europe/Moscow"}
    assert quiet_until(quiet, now) == datetime(2026, 11, 15, 8, tzinfo=zone).timestamp()
    assert quiet_until(quiet, datetime(2026, 11, 14, 12, tzinfo=zone).timestamp()) is None


async def test_quiet_defers_event_and_rechecks_mute(context):
    archive, _, _, _ = context
    await store(archive)
    await store(archive, update_id=2, edit=True, text="После")
    local = datetime.now(ZoneInfo("Europe/Moscow"))
    start = (local - timedelta(hours=1)).strftime("%H:%M")
    end = (local + timedelta(hours=1)).strftime("%H:%M")
    await archive.set_setting("quiet", {"start": start, "end": end, "timezone": "Europe/Moscow"})
    sender = worker(context)
    await sender.run_once()
    row = (await jobs(archive))[0]
    assert row["state"] == "pending" and row["available_at"] > time.time()
    assert sender.api.sent == []
    await archive.set_setting("muted", True)
    async with archive._transaction():
        await archive._db.execute("UPDATE jobs SET available_at=0")
    await sender.run_once()
    assert sender.api.sent == []
    assert (await jobs(archive))[0]["state"] == "done"


async def test_digest_groups_ready_events_and_links_histories(context):
    archive, _, _, _ = context
    await store(archive)
    await store(archive, update_id=2, edit=True, text="После")
    await deleted(archive)
    await archive.set_setting("digest_minutes", 10)
    sender = worker(context)
    assert await sender.run_once()
    assert await sender.run_once()
    assert sender.api.sent == []
    for row in await jobs(archive):
        await archive.set_setting(
            f"notification_due:{row['id']}", {"minutes": 10, "due": time.time() - 1}
        )
    async with archive._transaction():
        await archive._db.execute("UPDATE jobs SET available_at=0")
    await sender.run_once()
    assert len(sender.api.sent) == 1
    assert "Сводка событий: 2" in sender.api.sent[0][1]
    assert len(sender.api.sent[0][2]["reply_markup"]["inline_keyboard"]) == 2
    assert all(row["state"] == "done" for row in await jobs(archive))


@pytest.mark.parametrize(
    "error,state",
    [
        (TelegramError(0, category="transport"), "uncertain"),
        (TimeoutError(), "uncertain"),
        (TelegramError(403), "blocked"),
        (TelegramError(429, retry_after=70), "pending"),
        (TelegramError(503), "pending"),
    ],
)
async def test_delivery_failure_semantics_preserve_archived_content(context, error, state):
    archive, _, _, _ = context
    message_id = await store(archive)
    await store(archive, update_id=2, edit=True, text="После")
    sender = worker(context, FakeAPI(error))
    await sender.run_once()
    row = (await jobs(archive))[0]
    assert row["state"] == state
    assert len((await archive.history(message_id))["versions"]) == 2
    if error.__class__ is TelegramError and error.code == 429:
        assert row["available_at"] >= time.time() + 65
    if state in {"uncertain", "blocked"}:
        assert await sender.run_once() is False


async def test_formatting_error_falls_back_to_plain_text_once(context):
    archive, _, _, commands = context
    await commands.handle(command("/status"))
    sender = worker(context, FakeAPI(TelegramError(400)))
    await sender.run_once()
    assert sender.api.calls[0][0] == "sendMessage"
    assert "parse_mode" not in sender.api.calls[0][1]
    assert (await jobs(archive))[0]["state"] == "done"


async def test_delete_requires_expiring_owner_bound_nonce(context):
    archive, _, _, commands = context
    message_id = await store(archive)
    await commands.handle(command(f"/delete {message_id}"))
    confirmation = (await payloads(archive))[-1]
    data = confirmation["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await commands.handle(callback(data, update_id=101, owner=1))
    assert await archive.history(message_id)
    await commands.handle(callback(data, update_id=102))
    assert await archive.history(message_id) is None
    await commands.handle(callback(data, update_id=102))
    assert await archive.history(message_id) is None
    assert sum(row["dedupe_key"] == "ui:102:0" for row in await jobs(archive)) == 1


async def test_expired_delete_confirmation_does_not_erase(context):
    archive, _, _, commands = context
    message_id = await store(archive)
    await commands.handle(command(f"/delete {message_id}"))
    data = (await payloads(archive))[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    key = "ui_delete:" + data.split(":")[1]
    state = await archive.get_setting(key)
    await archive.set_setting(key, {**state, "expires": 0})
    await commands.handle(callback(data, update_id=101))
    assert await archive.history(message_id)


async def test_erase_while_sender_waits_for_lock_cancels_sensitive_send(context):
    archive, _, operations, commands = context
    message_id = await store(archive)
    await commands.handle(command(f"/message {message_id}"))
    sender = worker(context)
    async with operations.lock:
        task = asyncio.create_task(sender.run_once())
        for _ in range(100):
            if (await jobs(archive))[0]["state"] == "running":
                break
            await asyncio.sleep(0)
        assert (await jobs(archive))[0]["state"] == "running"
        await archive.erase_message(message_id)
    await task
    assert sender.api.sent == []


async def test_bookmark_export_search_and_deep_link(context):
    archive, _, _, commands = context
    message_id = await store(archive)
    for index, text in enumerate(
        [
            f"/bookmark {message_id}",
            f"/bookmark {message_id}",
            "/bookmarks",
            "/search До",
            "/search_chat 10 До",
            "/start bizChat10",
            "/export 10",
            "/export_media 10",
            "/backup",
        ]
    ):
        await commands.handle(command(text, update_id=100 + index))
    assert len(await archive.list_bookmarks()) == 1
    exports = await jobs(archive, "export")
    assert len(exports) == 3
    assert '"include_media":false' in exports[0]["payload"]
    assert '"include_media":true' in exports[1]["payload"]
    assert '"action":"backup"' in exports[2]["payload"]
    for payload in await payloads(archive):
        for row in payload.get("reply_markup", {}).get("inline_keyboard", []):
            assert all(len(item["callback_data"].encode()) <= 64 for item in row)


def test_threshold_never_hides_media_or_format_changes():
    before = {"text": "A" * 100, "payload": {"text": "A" * 100, "entities": []}}
    tiny = {"text": "A" * 99 + "B", "payload": {"text": "A" * 99 + "B", "entities": []}}
    assert not edit_visible(before, tiny, 10)
    changed = {
        **tiny,
        "payload": {**tiny["payload"], "entities": [{"type": "bold", "offset": 0, "length": 1}]},
    }
    assert edit_visible(before, changed, 100)


async def test_export_traversal_is_rejected_without_reading_file(context):
    archive, _, _, _ = context
    await archive.enqueue("notification", "bad-export", {"export_path": "../outside.sba"})
    sender = worker(context)
    await sender.run_once()
    assert sender.api.documents == []
    assert (await jobs(archive))[0]["state"] == "failed"


async def test_cancelled_send_expires_to_uncertain_not_automatic_retry(context):
    archive, _, _, commands = context
    await commands.handle(command("/status"))
    started = asyncio.Event()

    class SlowAPI(FakeAPI):
        async def send_text(self, *args, **options):
            started.set()
            await asyncio.Event().wait()

    sender = worker(context, SlowAPI())
    task = asyncio.create_task(sender.run_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await jobs(archive))[0]["state"] == "running"
    async with archive._transaction():
        await archive._db.execute("UPDATE jobs SET lease_until=0")
    assert not await sender.run_once()
    assert (await jobs(archive))[0]["state"] == "uncertain"


async def test_retry_command_replay_does_not_repeat_uncertain_send(context):
    archive, _, _, commands = context
    await archive.enqueue("notification", "ambiguous", {"text": "Private"})
    job = await archive.claim_job("notification")
    await archive.retry_job(job["id"], "transport", 0, state="uncertain", attempts=job["attempts"])
    await commands.handle(command("/retry_uncertain"))
    # A manually retried send also has an ambiguous result.
    job = await archive.claim_job("notification")
    await archive.retry_job(job["id"], "transport", 0, state="uncertain", attempts=job["attempts"])
    await commands.handle(command("/retry_uncertain"))
    assert (await jobs(archive))[0]["state"] == "uncertain"


async def test_send_pacing_reserves_one_second_between_attempts(context, monkeypatch):
    import spybot.ui

    sender = NotificationWorker(context[0], FakeAPI(), context[1], KEY, context[2])
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(spybot.ui.time, "monotonic", lambda: 100)
    monkeypatch.setattr(spybot.ui.asyncio, "sleep", sleep)
    await sender._paced()
    await sender._paced()
    assert delays == [1]
