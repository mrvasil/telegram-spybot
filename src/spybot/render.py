"""Bounded, escaped Russian views of the lossless archive.

Previews deliberately have a visible boundary. Full records remain available in
history pages and the portable JSONL/HTML export.
"""

from __future__ import annotations

import difflib
import html
import json
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


def content(version: dict) -> str:
    values = [version.get("text"), version.get("caption")]
    text = "\n".join(value for value in values if isinstance(value, str) and value)
    if text:
        return text
    raw = version.get("payload") or {}
    # Show unknown keys rather than pretending a new message type is empty.
    known_metadata = {
        "message_id",
        "date",
        "edit_date",
        "chat",
        "from",
        "sender_chat",
        "business_connection_id",
        "text",
        "caption",
        "entities",
        "caption_entities",
    }
    fields = [str(key) for key in raw if key not in known_metadata]
    if not fields:
        return "[Текст отсутствует; исходные поля сохранены в экспорте JSONL]"
    return (
        "[Структурированное сообщение: "
        + ", ".join(fields)
        + "]\n"
        + json.dumps({key: raw[key] for key in fields}, ensure_ascii=False, indent=2)
    )


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


def difference(previous: dict | None, current: dict, limit: int = 850) -> str:
    if previous is None:
        return "Предыдущая версия не была доступна боту."
    old, new = content(previous), content(current)
    if old == new:
        return "Изменены вложения, форматирование или другие поля. Полные поля — в JSONL."
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="до", tofile="после", lineterm=""
        )
    )
    return "Разница (− до, + после):\n" + preview(diff, limit)


def media_status(media: dict) -> str:
    states = {
        "saved": "сохранён",
        "pending": "в очереди",
        "failed": "ошибка; /retry",
        "unavailable": "недоступен Telegram",
        "too_large": "превышен лимит",
    }
    metadata = (media.get("metadata") or {}).get("file") or {}
    name = str(metadata.get("file_name") or media.get("kind") or "файл")
    return f"#{media['id']} {preview(name, 90)} — {states.get(media['state'], media['state'])}"


def history_text(history: dict, index: int, timezone: str) -> str:
    message, versions = history["message"], history["versions"]
    header = f"Запись #{message['id']} · {message.get('chat_title') or message['chat_id']}\n"
    header += f"Чат {message['chat_id']} · исходный номер {message['message_id']}\n"
    if message.get("sender_name") or message.get("sender_id"):
        header += f"Отправитель: {message.get('sender_name') or message['sender_id']}\n"
    if message.get("deleted_at") is not None:
        header += "Удалено в Telegram · " + timestamp(message["deleted_at"], timezone) + "\n"
        header += "Telegram не сообщает, кто удалил сообщение.\n"
    if not versions:
        return (
            header
            + "\nИсходник недоступен: бот получил только событие удаления.\nЭто запись о факте удаления, содержание восстановить нельзя."
        )
    version = versions[index]
    labels = {"original": "исходная", "edit": "редакция", "context": "снимок из контекста"}
    result = (
        header
        + f"\nВерсия {index + 1}/{len(versions)} · {labels.get(version['kind'], version['kind'])}\n"
    )
    result += timestamp(version["source_date"], timezone) + "\n\n" + preview(content(version), 1600)
    if version["kind"] == "edit":
        result += "\n\n" + difference(versions[index - 1] if index else None, version)
    if history["media"]:
        result += f"\n\nВложения всех версий: {len(history['media'])}. Откройте «Файлы»."
    result += "\n\nИстория показывает предпросмотр версии. Полные тексты, поля и события — «Экспорт записи»."
    return result


def event_text(history: dict, event: str, version_id: int | None, timezone: str) -> str:
    message, versions = history["message"], history["versions"]
    result = "Сообщение изменено" if event == "edit" else "Сообщение удалено в Telegram"
    result += f" · #{message['id']}\nЧат: {message.get('chat_title') or message['chat_id']}\n"
    index = next(
        (i for i, version in enumerate(versions) if version["id"] == version_id), len(versions) - 1
    )
    if event == "delete":
        result += "Telegram не сообщает, кто удалил сообщение.\n"
    if index < 0:
        return result + "Исходник недоступен; сохранён только факт удаления."
    current = versions[index]
    result += timestamp(current["source_date"], timezone) + "\n\n"
    if event == "edit":
        previous = versions[index - 1] if index else None
        result += "До:\n" + (
            preview(content(previous), 650) if previous else "[Исходник недоступен]"
        )
        result += "\n\nПосле:\n" + preview(content(current), 650)
        result += "\n\n" + difference(previous, current, 500)
    else:
        result += "Последняя сохранённая версия:\n" + preview(content(current), 1200)
    if history["media"]:
        result += "\n\nФайлы: " + "; ".join(media_status(media) for media in history["media"][:4])
        if len(history["media"]) > 4:
            result += f"; ещё {len(history['media']) - 4} — в истории."
    return result + "\n\nПолная история и файлы — по кнопке ниже."
