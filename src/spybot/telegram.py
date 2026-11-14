"""Raw Bot API adapter: retain new/unknown JSON fields without SDK coercion."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import aiohttp


class TelegramError(Exception):
    def __init__(self, code: int, retry_after: int = 0, category: str = "telegram"):
        self.code = code
        self.retry_after = retry_after
        self.category = category
        # Never retain description/request URL: they can contain private content or tokens.
        super().__init__(f"{category}:{code}")


class TelegramAPI:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org"):
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._files = f"{base_url.rstrip('/')}/file/bot{token}/"
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> TelegramAPI:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=90, sock_connect=15, sock_read=75),
            raise_for_status=False,
            trust_env=False,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.close()

    async def call(self, method: str, **parameters: Any) -> Any:
        if not self._session:
            raise RuntimeError("API client is not open")
        try:
            async with self._session.post(self._base + "/" + method, json=parameters) as response:
                result = await response.json(content_type=None)
                if not isinstance(result, dict):
                    raise TelegramError(502, category="protocol")
                if not result.get("ok"):
                    retry = (result.get("parameters") or {}).get("retry_after", 0)
                    description = str(result.get("description", "")).lower()
                    category = (
                        "file_too_large"
                        if "file is too big" in description
                        else "message_not_modified"
                        if "message is not modified" in description
                        else "telegram"
                    )
                    raise TelegramError(
                        int(result.get("error_code", response.status)), int(retry), category
                    )
                return result.get("result")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise TelegramError(0, category="transport") from None

    async def updates(self, offset: int | None, poll_timeout: int = 30) -> list[dict]:
        parameters: dict[str, Any] = {"timeout": poll_timeout, "limit": 100, "allowed_updates": []}
        if offset is not None:
            parameters["offset"] = offset
        updates = await self.call("getUpdates", **parameters)
        if not isinstance(updates, list) or any(
            not isinstance(u, dict) or not isinstance(u.get("update_id"), int) for u in updates
        ):
            raise TelegramError(502, category="protocol")
        return updates

    async def download(self, file_path: str) -> AsyncIterator[bytes]:
        if not self._session:
            raise RuntimeError("API client is not open")
        # file_path comes only from getFile, not an attachment/user-supplied URL.
        if file_path.startswith("/") or ".." in file_path.split("/"):
            raise TelegramError(400, category="local_file_path")
        try:
            async with self._session.get(
                self._files + quote(file_path, safe="/"),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90),
            ) as response:
                if response.status != 200:
                    raise TelegramError(response.status, category="download")
                async for chunk in response.content.iter_chunked(64 * 1024):
                    yield chunk
        except (aiohttp.ClientError, TimeoutError):
            raise TelegramError(0, category="download") from None

    async def send_text(self, chat_id: int, text: str, **options: Any) -> Any:
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            **options,
        )

    async def send_document(
        self, chat_id: int, chunks: AsyncIterator[bytes], filename: str, caption: str = ""
    ) -> Any:
        return await self.send_media("document", chat_id, chunks, filename, caption)

    async def send_media(
        self,
        kind: str,
        chat_id: int,
        chunks: AsyncIterator[bytes],
        filename: str,
        caption: str = "",
        *,
        reply_markup: dict | None = None,
        formatted: bool = False,
        protect_content: bool = False,
    ) -> Any:
        methods = {
            "photo": "sendPhoto",
            "video": "sendVideo",
            "animation": "sendAnimation",
            "audio": "sendAudio",
            "voice": "sendVoice",
            "video_note": "sendVideoNote",
            "sticker": "sendSticker",
            "document": "sendDocument",
        }
        if kind not in methods:
            raise ValueError("unsupported_media_kind")
        if not self._session:
            raise RuntimeError("API client is not open")
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption and kind not in {"sticker", "video_note"}:
            form.add_field("caption", caption)
            if formatted:
                form.add_field("parse_mode", "HTML")
        if protect_content:
            form.add_field("protect_content", "true")
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup))
        if kind == "video":
            form.add_field("supports_streaming", "true")
        form.add_field(kind, chunks, filename=filename, content_type="application/octet-stream")
        try:
            async with self._session.post(
                self._base + "/" + methods[kind],
                data=form,
                timeout=aiohttp.ClientTimeout(total=1800, sock_connect=15, sock_read=90),
            ) as response:
                result = await response.json(content_type=None)
                if not result.get("ok"):
                    raise TelegramError(
                        int(result.get("error_code", response.status)),
                        int((result.get("parameters") or {}).get("retry_after", 0)),
                    )
                return result.get("result")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise TelegramError(0, category="transport") from None


async def retry_delay(error: TelegramError, attempts: int) -> None:
    await asyncio.sleep(max(error.retry_after, min(2 ** min(attempts, 8), 300)))
