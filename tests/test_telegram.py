import aiohttp.web
import pytest

from spybot.telegram import TelegramAPI, TelegramError


@pytest.fixture
async def fake_telegram():
    responses = {}
    calls = []

    async def route(request):
        payload = await request.json()
        calls.append((request.match_info["method"], payload))
        return aiohttp.web.json_response(
            responses.get(request.match_info["method"], {"ok": True, "result": []})
        )

    app = aiohttp.web.Application()
    app.router.add_post("/bot123:synthetic/{method}", route)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", responses, calls
    await runner.cleanup()


async def test_raw_updates_retain_unknown_fields(fake_telegram):
    url, responses, calls = fake_telegram
    updates = [
        {"update_id": 19, "business_message": {"rich_future_content": {"secret_spoiler": "x"}}}
    ]
    responses["getUpdates"] = {"ok": True, "result": updates}
    async with TelegramAPI("123:synthetic", url) as api:
        assert await api.updates(15, 1) == updates
    assert calls == [
        ("getUpdates", {"offset": 15, "timeout": 1, "limit": 100, "allowed_updates": []})
    ]


async def test_error_never_exposes_description_or_token(fake_telegram):
    url, responses, _ = fake_telegram
    responses["sendMessage"] = {
        "ok": False,
        "error_code": 429,
        "description": "PRIVATE CONTENT and TOKEN",
        "parameters": {"retry_after": 17},
    }
    async with TelegramAPI("123:synthetic", url) as api:
        with pytest.raises(TelegramError) as error:
            await api.send_text(1, "PRIVATE")
    assert error.value.retry_after == 17
    assert str(error.value) == "telegram:429"
    assert "PRIVATE" not in repr(error.value)


async def test_non_update_response_is_rejected(fake_telegram):
    url, responses, _ = fake_telegram
    responses["getUpdates"] = {"ok": True, "result": [{"message": {}}]}
    async with TelegramAPI("123:synthetic", url) as api:
        with pytest.raises(TelegramError):
            await api.updates(None)
