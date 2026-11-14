"""Run the actual daemon against a local HTTP Bot API, never Telegram."""

import asyncio
import os
import signal
import sys

import aiohttp.web

from spybot.archive import Archive


async def test_daemon_archives_and_downloads_before_delete_without_real_telegram(tmp_path):
    token = "123:synthetic_token_not_a_real_token"
    key = bytes(range(32))
    key_file = tmp_path / "archive.key"
    key_file.write_text(key.hex())
    key_file.chmod(0o600)
    data = tmp_path / "data"
    raw = {
        "business_connection_id": "c",
        "message_id": 10,
        "date": 100,
        "chat": {"id": 20, "type": "private"},
        "from": {"id": 20},
        "text": "ORIGINAL_SPOILER",
        "entities": [{"type": "spoiler", "offset": 0, "length": 16}],
        "photo": [{"file_id": "photo", "file_size": 7}],
        "future_field": {"hidden": "preserve"},
    }
    updates = [
        {
            "update_id": 1,
            "business_connection": {"id": "c", "user": {"id": 99}, "is_enabled": True},
        },
        {"update_id": 2, "business_message": raw},
        {"update_id": 3, "edited_business_message": {**raw, "text": "EDITED", "edit_date": 101}},
        {
            "update_id": 4,
            "deleted_business_messages": {
                "business_connection_id": "c",
                "chat": {"id": 20},
                "message_ids": [10],
            },
        },
        {
            "update_id": 5,
            "message": {
                "from": {"id": 99},
                "chat": {"id": 99, "type": "private"},
                "text": "/status",
            },
        },
    ]
    calls = []
    offsets = []
    downloads = []
    delivered = asyncio.Event()

    async def api(request):
        method = request.match_info["method"]
        if request.content_type.startswith("multipart/"):
            form = await request.multipart()
            parameters = {}
            async for part in form:
                parameters[part.name] = (
                    bytes(await part.read()) if part.filename else await part.text()
                )
        else:
            parameters = await request.json()
        calls.append((method, parameters))
        if method == "getMe":
            result = {"id": 123, "is_bot": True}
        elif method == "getWebhookInfo":
            result = {"url": ""}
        elif method == "getUpdates":
            offsets.append(parameters.get("offset"))
            if parameters.get("offset", 0) <= 1:
                result = updates
            else:
                await asyncio.sleep(0.1)
                result = []
        elif method == "getFile":
            result = {"file_path": "photo.bin", "file_size": 7}
        elif method in {"sendMessage", "sendPhoto"}:
            result = {"message_id": len(calls)}
            if len([c for c in calls if c[0] in {"sendMessage", "sendPhoto"}]) >= 4:
                delivered.set()
        else:
            result = True
        return aiohttp.web.json_response({"ok": True, "result": result})

    async def download(request):
        downloads.append(True)
        return aiohttp.web.Response(body=b"content")

    app = aiohttp.web.Application()
    app.router.add_post("/bot" + token + "/{method}", api)
    app.router.add_get("/file/bot" + token + "/photo.bin", download)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    env = {
        **os.environ,
        "TOKEN": token,
        "USER_ID": "99",
        "DATA_DIR": str(data),
        "ARCHIVE_KEY_FILE": str(key_file),
        "BOT_API_URL": f"http://127.0.0.1:{port}",
        "BACKUP_HOURS": "0",
        "MIN_FREE_BYTES": "0",
        "POLL_TIMEOUT": "1",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "spybot",
        "--env-file",
        str(tmp_path / "absent.env"),
        "run",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(delivered.wait(), timeout=20)
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        await runner.cleanup()
    assert process.returncode == 0, (stdout, stderr)
    assert token.encode() not in stdout + stderr
    assert b"ORIGINAL_SPOILER" not in stdout + stderr
    assert offsets[0] is None and 6 in offsets
    assert len(downloads) == 2
    archive = await Archive.open(data / "archive.db", key, 99)
    try:
        history = await archive.history(1)
        assert history["message"]["deleted_at"] is not None
        assert [v["text"] for v in history["versions"]] == ["ORIGINAL_SPOILER", "EDITED"]
        assert history["versions"][0]["payload"]["future_field"]["hidden"] == "preserve"
        assert all(m["state"] == "saved" for m in history["media"])
        assert (await archive.stats())["inbox_pending"] == 0
    finally:
        await archive.close()
