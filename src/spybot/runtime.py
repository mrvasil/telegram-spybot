"""Independent intake, projection, download, notification and maintenance loops."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from spybot.archive import Archive, ForeignConnection, UnknownConnection
from spybot.config import Settings
from spybot.locking import InstanceLock
from spybot.media import MediaWorker
from spybot.telegram import TelegramAPI, TelegramError, retry_delay

logger = logging.getLogger(__name__)


async def poll_loop(archive: Archive, api: TelegramAPI, settings: Settings, health: dict) -> None:
    failures = 0
    while True:
        try:
            updates = await api.updates(await archive.get_offset(), settings.poll_timeout)
            if updates:
                # Never send the next offset to Telegram before this transaction commits.
                await archive.ingest_batch(updates)
                health["last_received_at"] = time.time()
            health["last_poll_ok"] = time.time()
            failures = 0
            health.pop("poll_error", None)
        except TelegramError as error:
            if error.code in {401, 409}:
                # Wrong token / another poller must be fixed, not hidden by infinite retry.
                raise
            failures += 1
            health["poll_error"] = str(error)
            logger.warning("poll_retry category=%s code=%d", error.category, error.code)
            await retry_delay(error, failures)


async def process_once(archive: Archive, api: TelegramAPI, commands: Any) -> bool:
    pending = await archive.pending_updates(limit=50)
    if not pending:
        return False
    for item in pending:
        update = item["payload"]
        try:
            business_keys = {
                "business_connection",
                "business_message",
                "edited_business_message",
                "deleted_business_messages",
            }
            if business_keys.intersection(update):
                try:
                    await archive.apply_update(update)
                except UnknownConnection as unknown:
                    connection = await api.call(
                        "getBusinessConnection", business_connection_id=unknown.connection_id
                    )
                    try:
                        await archive.remember_connection(connection)
                    except ForeignConnection:
                        pass  # Archive records the rejected connection; apply marks ignored.
                    await archive.apply_update(update)
            else:
                await commands.handle(update)
                await archive.mark_processed(item["update_id"])
        except asyncio.CancelledError:
            raise
        except TelegramError as error:
            delay = max(error.retry_after, min(2 ** min(item["attempts"] + 1, 9), 600))
            await archive.fail_update(item["update_id"], str(error), time.time() + delay)
        except Exception as error:
            # Preserve the raw update for repair/replay; unrelated updates still proceed.
            # Exception strings/tracebacks can contain private JSON, so only log type+ID.
            logger.error(
                "projection_retry update_id=%s error_type=%s",
                item["update_id"],
                type(error).__name__,
            )
            await archive.fail_update(item["update_id"], type(error).__name__, time.time() + 60)
    return True


async def process_loop(archive: Archive, api: TelegramAPI, commands: Any) -> None:
    while True:
        if not await process_once(archive, api, commands):
            await asyncio.sleep(0.2)


def _write_health(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def health_loop(archive: Archive, settings: Settings, health: dict) -> None:
    while True:
        health["updated_at"] = time.time()
        health["stats"] = await archive.stats()
        await asyncio.to_thread(_write_health, settings.data_dir / "health.json", health.copy())
        await asyncio.sleep(10)


async def run(settings: Settings) -> None:
    # Read/validate an existing key before making directories or network requests.
    key = settings.read_key()
    settings.prepare_data_dir()
    with InstanceLock(settings.data_dir / ".instance.lock"):
        archive = await Archive.open(settings.data_dir / "archive.db", key, settings.owner_id)
        health = {"state": "starting", "started_at": time.time()}
        try:
            async with TelegramAPI(settings.token, settings.api_url) as api:
                me = await api.call("getMe")
                await archive.bind_identity(me["id"])
                webhook = await api.call("getWebhookInfo")
                if webhook.get("url"):
                    raise RuntimeError(
                        "У бота установлен webhook. Сначала согласованно переключите его на polling без удаления updates"
                    )
                # Imported here to keep offline commands independent of runtime composition.
                from spybot.operations import Operations
                from spybot.ui import Commands, NotificationWorker

                maintenance_lock = asyncio.Lock()
                operations = Operations(archive, settings, key, maintenance_lock)
                commands = Commands(archive, settings, operations)
                notifications = NotificationWorker(archive, api, settings, key, operations)
                worker = MediaWorker(archive, api, settings, key, maintenance_lock)
                health["state"] = "running"
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, stop.set)
                tasks: list[asyncio.Task] = []
                try:
                    async with asyncio.TaskGroup() as group:
                        tasks.append(group.create_task(poll_loop(archive, api, settings, health)))
                        tasks.append(group.create_task(process_loop(archive, api, commands)))
                        tasks.append(group.create_task(notifications.run()))
                        tasks.append(group.create_task(operations.run()))
                        tasks.append(group.create_task(health_loop(archive, settings, health)))
                        for _ in range(settings.media_workers):
                            tasks.append(group.create_task(worker.run()))
                        await stop.wait()
                        for task in tasks:
                            task.cancel()
                finally:
                    for sig in (signal.SIGTERM, signal.SIGINT):
                        loop.remove_signal_handler(sig)
        finally:
            health["state"] = "stopped"
            health["updated_at"] = time.time()
            await asyncio.to_thread(_write_health, settings.data_dir / "health.json", health)
            await archive.close()
