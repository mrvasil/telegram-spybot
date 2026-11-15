"""Bounded, escaped Russian views of the lossless archive.

Previews deliberately have a visible boundary. Full records remain available in
history pages and the portable JSONL/HTML export.
"""

from __future__ import annotations

import difflib
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

TEXT_LIMIT = 3900


def escaped_parts(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Escape after splitting at character boundaries, including UTF-16 accounting.

    Counting the escaped representation is stricter than Telegram's decoded
    limit and ensures an entity and a surrogate pair never straddle messages.
    """
    if limit < 12:
        raise ValueError("text_limit_too_small")
    result: list[str] = []
    current: list[str] = []
    size = 0
    for character in str(text):
        escaped = html.escape(character, quote=False)
        width = len(escaped.encode("utf-16-le")) // 2
        if size + width > limit:
            result.append("".join(current))
            current, size = [], 0
        current.append(escaped)
        size += width
    if current or not result:
        result.append("".join(current) or " ")
    return result


def timestamp(value: float | None, timezone: str) -> str:
    if value is None:
        return "время неизвестно"
    try:
        return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M:%S %Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return "время неизвестно"


def preview(value: str | None, limit: int = 700) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[…предпросмотр; полная запись — в экспорте]"


MEDIA_LABELS = {
    "photo": "Фото",
    "video": "Видео",
    "animation": "GIF",
    "voice": "Голосовое",
    "video_note": "Видеосообщение",
    "audio": "Аудио",
    "document": "Файл",
    "sticker": "Стикер",
}


def safe(value: object, limit: int = 1600) -> str:
    """Escape user content and bound UTF-16/HTML length without cutting tags/entities."""
    parts = escaped_parts(str(value), limit)
    return parts[0] + ("…" if len(parts) > 1 else "")


def primary_media(history: dict, version_id: int | None = None) -> list[dict]:
    """Visible attachments, excluding thumbnails and alternate photo sizes."""
    if version_id is None and history["versions"]:
        version_id = history["versions"][-1]["id"]
    rows = [m for m in history["media"] if m["version_id"] == version_id]
    result, photos = [], []
    for media in rows:
        path = (media.get("metadata") or {}).get("path", "")
        if re.fullmatch(r"/photo/\d+", path):
            photos.append(media)
        elif path in {"/" + kind for kind in MEDIA_LABELS if kind != "photo"}:
            result.append(media)
    if photos:

        def quality(media):
            data = (media.get("metadata") or {}).get("file") or {}
            return (data.get("width", 0) * data.get("height", 0), data.get("file_size", 0))

        result.insert(0, max(photos, key=quality))
    return result


def deletion_media(history: dict) -> list[dict]:
    """One full-size photo per version, all other non-thumbnail files, no repeats."""
    selected: list[dict] = []
    seen: dict[tuple, int] = {}
    for version in reversed(history["versions"]):
        rows = [m for m in history["media"] if m["version_id"] == version["id"]]
        primary = primary_media(history, version["id"])
        photo_rows = [
            m
            for m in rows
            if re.fullmatch(r"/photo/\d+", (m.get("metadata") or {}).get("path", ""))
        ]
        # If Telegram refused the original, still send the best available copy.
        if photo_rows and not any(
            m["state"] in {"saved", "pending"} for m in primary if m["kind"] == "photo"
        ):
            saved = [m for m in photo_rows if m["state"] == "saved"]
            if saved:
                best = max(
                    saved,
                    key=lambda m: (
                        (m.get("metadata") or {}).get("file", {}).get("width", 0)
                        * (m.get("metadata") or {}).get("file", {}).get("height", 0)
                    ),
                )
                primary = [m for m in primary if m["kind"] != "photo"] + [best]
        primary_ids = {m["id"] for m in primary}
        for media in rows:
            path = (media.get("metadata") or {}).get("path", "")
            if media["id"] in primary_ids or (
                media not in photo_rows
                and not any(p in path.split("/") for p in ("thumbnail", "thumb", "cover"))
            ):
                identity = (media["kind"], media.get("file_unique_id") or media["file_id"])
                if identity not in seen:
                    seen[identity] = len(selected)
                    selected.append(media)
                elif selected[seen[identity]]["state"] != "saved" and media["state"] == "saved":
                    selected[seen[identity]] = media
    return selected


def deletion_caption(history: dict, media: dict) -> str:
    message = history["message"]
    source = next(v for v in history["versions"] if v["id"] == media["version_id"])
    title = safe(message.get("chat_title") or message["chat_id"], 160)
    caption = source.get("caption") or source.get("text") or ""
    return f"<b>Удалено</b> · {title}" + ("\n\n" + safe(caption, 650) if caption else "")


def content(version: dict) -> str:
    raw = version.get("payload") or {}
    values = [version.get("text"), version.get("caption")]
    text = "\n".join(value for value in values if isinstance(value, str) and value)
    labels = [label for key, label in MEDIA_LABELS.items() if raw.get(key)]
    if labels:
        return " · ".join(labels) + ("\n" + text if text else "")
    if text:
        return text
    if raw.get("poll"):
        return "Опрос: " + str(raw["poll"].get("question", ""))
    if raw.get("contact"):
        contact = raw["contact"]
        return (
            "Контакт: "
            + " ".join(
                str(contact.get(k) or "") for k in ("first_name", "last_name", "phone_number")
            ).strip()
        )
    if raw.get("location") or raw.get("venue"):
        return "Геопозиция" + (
            ": " + str(raw["venue"].get("title", "")) if raw.get("venue") else ""
        )
    if raw.get("dice"):
        return str(raw["dice"].get("emoji", "🎲")) + " " + str(raw["dice"].get("value", ""))
    return "Сообщение без текста · содержимое доступно в экспорте"


def changed_envelope(version: dict) -> dict:
    """Compare media, entities and future fields independently of text edits."""
    return {
        key: value
        for key, value in (version.get("payload") or {}).items()
        if key not in {"text", "caption", "edit_date", "date"}
    }


def edit_visible(previous: dict | None, current: dict, threshold: int) -> bool:
    if previous is None or changed_envelope(previous) != changed_envelope(current):
        return True
    old = (previous.get("text") or "") + "\n" + (previous.get("caption") or "")
    new = (current.get("text") or "") + "\n" + (current.get("caption") or "")
    if old == new:
        return False
    return (1 - difflib.SequenceMatcher(None, old, new, autojunk=False).ratio()) * 100 >= threshold


def difference(previous: dict | None, current: dict, limit: int = 900) -> str:
    new = safe(content(current), limit)
    if previous is None:
        return f"<b>Стало</b>\n<blockquote>{new}</blockquote>\n<i>Предыдущая версия недоступна.</i>"
    old = safe(content(previous), limit)
    if content(previous) == content(current):
        return f"<blockquote>{new}</blockquote>\n<i>Изменены вложения или оформление.</i>"
    return (
        f"<b>Было</b>\n<blockquote>{old}</blockquote>\n<b>Стало</b>\n<blockquote>{new}</blockquote>"
    )


def media_status(media: dict) -> str:
    states = {
        "saved": "сохранено",
        "pending": "загружается",
        "failed": "не удалось скачать",
        "unavailable": "Telegram не отдал файл",
        "too_large": "превышен лимит загрузки",
    }
    metadata = (media.get("metadata") or {}).get("file") or {}
    name = str(metadata.get("file_name") or MEDIA_LABELS.get(str(media.get("kind")), "Вложение"))
    return f"{preview(name, 70)} · {states.get(media['state'], 'ожидает загрузки')}"


def _header(history: dict, title: str, date: float | None, timezone: str) -> str:
    message = history["message"]
    chat = safe(message.get("chat_title") or message["chat_id"], 180)
    parts = timestamp(date, timezone).split()
    date_text = f"{parts[0]} · {parts[1][:5]}" if len(parts) >= 3 else "время неизвестно"
    return f"<b>{title}</b> · {chat}\n<i>{safe(date_text, 80)}</i>"


def _media_summary(history: dict, version_id: int | None) -> str:
    media = primary_media(history, version_id)
    if not media:
        return ""
    states = ["Сохранено" if m["state"] == "saved" else media_status(m) for m in media]
    return "\n\n<i>" + safe(" · ".join(dict.fromkeys(states)), 260) + "</i>"


def history_text(history: dict, index: int, timezone: str) -> str:
    message, versions = history["message"], history["versions"]
    if not versions:
        return (
            _header(history, "Удалено", message.get("deleted_at"), timezone)
            + "\n\nИсходник недоступен — бот получил только событие удаления."
        )
    version = versions[index]
    title = "Удалено" if message.get("deleted_at") is not None else "Сообщение"
    result = _header(history, title, version["source_date"], timezone)
    if len(versions) > 1:
        result += f" · <i>Версия {index + 1} из {len(versions)}</i>"
    result += "\n\n"
    if version["kind"] == "edit":
        result += difference(versions[index - 1] if index else None, version)
    else:
        result += f"<blockquote>{safe(content(version))}</blockquote>"
    return result + _media_summary(history, version["id"])


def event_text(history: dict, event: str, version_id: int | None, timezone: str) -> str:
    message, versions = history["message"], history["versions"]
    index = next(
        (i for i, version in enumerate(versions) if version["id"] == version_id), len(versions) - 1
    )
    title = "Изменено" if event == "edit" else "Удалено"
    date = (
        message.get("deleted_at")
        if event == "delete"
        else versions[index]["source_date"]
        if index >= 0
        else None
    )
    result = _header(history, title, date, timezone)
    if index < 0:
        return result + "\n\nИсходник недоступен — бот получил только событие удаления."
    current = versions[index]
    result += "\n\n"
    if event == "edit":
        result += difference(versions[index - 1] if index else None, current)
    else:
        result += f"<blockquote>{safe(content(current), 1200)}</blockquote>"
    return result + _media_summary(history, current["id"])
