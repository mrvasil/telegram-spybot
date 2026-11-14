"""Lossless helpers for raw Bot API dictionaries (no SDK schema filtering)."""

from collections.abc import Iterator
from typing import Any


def chat_title(chat: dict) -> str | None:
    return (
        chat.get("title")
        or " ".join(str(chat[k]) for k in ("first_name", "last_name") if chat.get(k))
        or chat.get("username")
        or None
    )


def sender(raw: dict) -> tuple[int | None, str | None]:
    user = raw.get("from") or raw.get("sender_chat") or {}
    return user.get("id"), chat_title(user)


def collect_media(raw: Any, *, skip_context: bool = False) -> list[dict]:
    """Find *every* file occurrence, preserving metadata and its JSON path.

    A repeated file identifier at distinct paths remains a distinct record. This
    captures thumbnails, all photo sizes and future Bot API shapes without a
    whitelist. Identifiers without a file_id cannot be downloaded.
    """
    found: list[dict] = []

    def walk(value: Any, path: str, kind: str) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("file_id"), str) and value["file_id"]:
                found.append(
                    {
                        "file_id": value["file_id"],
                        "file_unique_id": value.get("file_unique_id"),
                        "kind": kind or "unknown",
                        "metadata": {"path": path or "/", "file": value},
                    }
                )
            for key, item in value.items():
                if (
                    skip_context
                    and key in ("reply_to_message", "pinned_message")
                    and isinstance(item, dict)
                    and isinstance(item.get("chat"), dict)
                    and isinstance(item["chat"].get("id"), int)
                    and isinstance(item.get("message_id"), int)
                ):
                    continue
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                walk(item, f"{path}/{escaped}", str(key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}/{index}", kind)

    walk(raw, "", "")
    return found


def contextual_messages(raw: Any, path: str = "") -> Iterator[tuple[str, dict]]:
    """Yield nested Telegram messages that have explicit chat and message IDs."""
    if isinstance(raw, dict):
        for key, value in raw.items():
            child_path = f"{path}/{key}"
            if key in ("reply_to_message", "pinned_message") and isinstance(value, dict):
                if (
                    isinstance(value.get("chat"), dict)
                    and isinstance(value["chat"].get("id"), int)
                    and isinstance(value.get("message_id"), int)
                ):
                    yield child_path, value
            yield from contextual_messages(value, child_path)
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            yield from contextual_messages(value, f"{path}/{index}")


def quote_search(query: str) -> str:
    """Treat input as literal FTS phrases, never FTS operators or syntax."""
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in query.split())
