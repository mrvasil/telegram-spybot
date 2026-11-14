"""Owner-only personal bot commands and fenced durable notification delivery."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from spybot.async_utils import filesystem_call
from spybot.crypto import verify_object
from spybot.media import decrypted_stream, object_path
from spybot.render import (
    edit_visible,
    escaped_parts,
    event_text,
    history_text,
    media_status,
    preview,
    timestamp,
)
from spybot.telegram import TelegramError

logger = logging.getLogger(__name__)
PAGE = 8


def button(label: str, data: str) -> dict:
    if len(data.encode()) > 64:
        raise ValueError("callback_too_long")
    return {"text": label, "callback_data": data}


def keyboard(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def integer(value: str, *, positive: bool = False) -> int:
    if not re.fullmatch(r"-?\d{1,18}", value):
        raise ValueError("Требуется целый числовой ID.")
    number = int(value)
    if positive and number <= 0:
        raise ValueError("ID записи должен быть положительным.")
    return number


def main_keyboard() -> dict:
    return keyboard(
        [
            [button("🗂 Архив", "list:a:0:0"), button("🗑 Удалённые", "list:d:0:0")],
            [button("💬 Чаты", "chats:0"), button("⭐ Закладки", "bookmarks:0")],
            [button("📊 Статус", "status"), button("⚙️ Настройки", "settings:g")],
            [button("📦 Экспорт", "export:all"), button("❔ Помощь", "help")],
        ]
    )


HELP = """Личный архив Telegram

1. Откройте Telegram → Настройки → Чат-боты → Секретарский режим (Secretary Mode, ранее Business Mode), подключите этого бота и выберите нужные чаты.
2. Разрешите боту получать сообщения выбранных чатов. После подключения проверьте /status и /chats.
3. Бот сохраняет доступные исходные сообщения, редакции, события удаления, вложения и неизвестные поля API. История до подключения автоматически не появляется. Если пришло только удаление, исходник недоступен. Ограничения Telegram и доступ к выбранным чатам определяют полноту архива.

Бот доступен только владельцу, заданному USER_ID, в личном чате. Проверки Telegram Premium нет. Настройки уведомлений не останавливают архивирование.

/archive · /deleted · /chats · /chat ID_чата
/message ID или /ID — история записи
/search слова · /search_chat ID_чата слова — поиск по всем версиям
/bookmarks · /bookmark ID · /unbookmark ID
/settings [ID_чата] · /mute [ID_чата] · /unmute [ID_чата]
/ignore 0..100 — порог мелких текстовых правок в процентах; вложения и форматирование учитываются отдельно
/quiet 23:00-08:00 [Europe/Moscow] · /quiet off
/digest N — сводка каждые N минут; 0 — сразу
/retention N — автоудаление локальных записей старше N дней; 0 — хранить всегда (по умолчанию)
/export [ID_чата] — полные JSONL + HTML без файлов
/export_media [ID_чата] — явно включить сохранённые файлы
/backup — зашифрованная локальная резервная копия
/retry — повторить безопасно завершившиеся ошибкой задания
/retry_uncertain — явно повторить неопределённые отправки; возможны дубликаты
/delete ID — удалить локальную запись после подтверждения

Удаление в Telegram не удаляет копию в архиве. Локальное стирание не удаляет старые резервные копии и уже отправленные уведомления. Экспорт в Telegram передаёт расшифрованный ZIP только владельцу; локальный файл остаётся зашифрованным."""


class Commands:
    def __init__(self, archive, settings, operations):
        self.archive, self.settings, self.operations = archive, settings, operations

    def _owner(self, message: dict, sender: dict) -> bool:
        chat = message.get("chat")
        return (
            isinstance(chat, dict)
            and chat.get("type") == "private"
            and type(chat.get("id")) is int
            and chat["id"] == self.settings.owner_id
            and type(sender.get("id")) is int
            and sender["id"] == self.settings.owner_id
        )

    async def _reply(
        self,
        update_id: int,
        text: str,
        markup: dict | None = None,
        *,
        message_id: int | None = None,
        sensitive: bool = False,
    ) -> None:
        parts = escaped_parts(text)
        for part, value in enumerate(parts):
            payload: dict = {"text": value}
            if sensitive:
                payload["archive_sensitive"] = True
            if message_id is not None:
                payload["message_id"] = message_id
            if markup and part == len(parts) - 1:
                payload["reply_markup"] = markup
            await self.archive.enqueue("notification", f"ui:{update_id}:{part}", payload)

    async def handle(self, update: dict) -> None:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            return
        update_id = update["update_id"]
        callback = update.get("callback_query")
        if callback is not None:
            if not isinstance(callback, dict) or callback.get("inline_message_id"):
                return
            message, sender = callback.get("message"), callback.get("from")
            if (
                not isinstance(message, dict)
                or not isinstance(sender, dict)
                or not self._owner(message, sender)
            ):
                return
            callback_id = callback.get("id")
            if isinstance(callback_id, str) and callback_id:
                await self.archive.enqueue(
                    "notification", f"ui:{update_id}:ack", {"answer_callback_query": callback_id}
                )
            data = callback.get("data")
            if not isinstance(data, str) or len(data.encode()) > 64:
                await self._reply(update_id, "Некорректная кнопка. Откройте /start.")
                return
            try:
                await self._callback(update_id, data)
            except (ValueError, IndexError, ZoneInfoNotFoundError):
                await self._reply(
                    update_id, "Кнопка недействительна или устарела. Откройте /start."
                )
            return
        message = update.get("message")
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("from"), dict)
            or not self._owner(message, message["from"])
        ):
            return
        text = message.get("text")
        if not isinstance(text, str):
            await self._reply(
                update_id,
                "Это личный пульт архива. Используйте кнопки или /help. Вложения из личного чата с ботом не добавляются в архив выбранных чатов.",
                main_keyboard(),
            )
            return
        try:
            await self._command(update_id, text.strip())
        except (ValueError, ZoneInfoNotFoundError) as error:
            # Only our validation messages are exposed, never database/API exceptions.
            await self._reply(
                update_id,
                str(error)
                if type(error) is ValueError
                else "Неизвестный часовой пояс. Пример: Europe/Moscow.",
            )

    async def _command(self, update_id: int, text: str) -> None:
        words = text.split(maxsplit=1)
        command = words[0].split("@", 1)[0].lower() if words else ""
        argument = words[1].strip() if len(words) > 1 else ""
        if command in {"/start", "/help"}:
            if argument.startswith("bizChat") and re.fullmatch(r"bizChat-?\d{1,18}", argument):
                await self._messages(update_id, "c", integer(argument[7:]), 0)
            else:
                await self._reply(update_id, HELP, main_keyboard())
        elif command == "/status":
            await self._status(update_id)
        elif command == "/chats":
            await self._chats(update_id, 0)
        elif command in {"/archive", "/deleted"}:
            await self._messages(update_id, "d" if command == "/deleted" else "a", 0, 0)
        elif command == "/chat":
            await self._messages(update_id, "c", integer(argument), 0)
        elif command == "/message" or re.fullmatch(r"/\d{1,18}", command):
            await self._history(
                update_id,
                integer(argument if command == "/message" else command[1:], positive=True),
            )
        elif command in {"/search", "/search_chat"}:
            chat_id = None
            if command == "/search_chat":
                pieces = argument.split(maxsplit=1)
                if len(pieces) != 2:
                    raise ValueError("Используйте /search_chat ID_чата слова.")
                chat_id, argument = integer(pieces[0]), pieces[1]
            if not argument or len(argument) > 2000:
                raise ValueError("Используйте /search слова (до 2000 символов).")
            token = secrets.token_hex(8)
            await self.archive.set_setting(
                f"ui_search:{token}",
                {"query": argument, "chat_id": chat_id, "expires": time.time() + 86400},
            )
            await self._search(update_id, token, 0)
        elif command == "/settings":
            await self._settings(update_id, integer(argument) if argument else None)
        elif command in {"/mute", "/unmute"}:
            chat_id = integer(argument) if argument else None
            await self.archive.set_setting("muted", command == "/mute", chat_id)
            await self._reply(
                update_id,
                ("Уведомления отключены" if command == "/mute" else "Уведомления включены")
                + (f" для чата {chat_id}." if chat_id is not None else " глобально.")
                + " Архивирование продолжается.",
            )
        elif command == "/ignore":
            number = integer(argument)
            if not 0 <= number <= 100:
                raise ValueError("Используйте /ignore N, где N от 0 до 100 (% изменения текста).")
            await self.archive.set_setting("ignore_threshold", number)
            await self._reply(
                update_id,
                f"Порог текстовых правок: {number}%. Все версии сохраняются; вложения и форматирование учитываются отдельно.",
            )
        elif command == "/digest":
            number = integer(argument)
            if not 0 <= number <= 10080:
                raise ValueError("Используйте /digest N: 0 — сразу, 1..10080 — интервал в минутах.")
            await self.archive.set_setting("digest_minutes", number)
            await self._reply(
                update_id,
                f"Сводка каждые {number} мин."
                if number
                else "Новые события отправляются сразу. Ожидающие события будут перепроверены при следующей попытке отправки.",
            )
        elif command == "/quiet":
            if argument.lower() == "off":
                await self.archive.set_setting("quiet", None)
                await self._reply(update_id, "Тихие часы отключены.")
            else:
                values = argument.split()
                if (
                    not values
                    or len(values) > 2
                    or not re.fullmatch(
                        r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", values[0]
                    )
                ):
                    raise ValueError(
                        "Используйте /quiet 23:00-08:00 [Europe/Moscow] или /quiet off."
                    )
                start, end = values[0].split("-")
                if start == end:
                    raise ValueError("Начало и конец тихих часов должны различаться.")
                zone = values[1] if len(values) == 2 else self.settings.timezone
                ZoneInfo(zone)
                await self.archive.set_setting(
                    "quiet", {"start": start, "end": end, "timezone": zone}
                )
                await self._reply(
                    update_id,
                    f"Тихие часы: {start}–{end}, {zone}. События ждут в очереди; команды доступны сразу.",
                )
        elif command == "/retention":
            days = integer(argument)
            if not 0 <= days <= 365000:
                raise ValueError("Используйте /retention N: число дней ≥ 0, 0 — хранить всегда.")
            await self.archive.set_setting("retention_days", days)
            await self._reply(
                update_id,
                f"Вы явно включили автоматическое локальное стирание записей старше {days} дней. Старые резервные копии и отправленные уведомления остаются."
                if days
                else "Автостирание отключено: записи хранятся бессрочно.",
            )
        elif command in {"/bookmark", "/unbookmark"}:
            await self._bookmark(
                update_id, integer(argument, positive=True), command == "/bookmark"
            )
        elif command == "/bookmarks":
            await self._bookmarks(update_id, 0)
        elif command in {"/export", "/export_media"}:
            await self._export(
                update_id,
                chat_id=integer(argument) if argument else None,
                include_media=command == "/export_media",
            )
        elif command == "/backup":
            await self.archive.enqueue("export", f"ui:{update_id}:backup", {"action": "backup"})
            await self._reply(
                update_id,
                "Создание зашифрованной резервной копии поставлено в очередь. Ключ в копию не включается; сохраните его отдельно.",
            )
        elif command in {"/retry", "/retry_uncertain"}:
            uncertain = command == "/retry_uncertain"
            counts = await self.archive.retry_failed(
                include_uncertain=uncertain, dedupe_key=f"ui:{update_id}"
            )
            await self._reply(
                update_id,
                f"Повтор поставлен в очередь: задания {counts.get('jobs', 0)}, входящие события {counts.get('updates', 0)}.\n"
                + (
                    "Неопределённые отправки также будут повторены: Telegram мог уже принять их, возможны дубликаты."
                    if uncertain
                    else f"Неопределённые отправки не повторены ({counts.get('uncertain_notifications', 0)}). /retry_uncertain явно разрешает повтор с риском дубликатов."
                ),
            )
        elif command == "/delete":
            await self._delete_request(update_id, integer(argument, positive=True))
        else:
            await self._reply(
                update_id,
                "Команда не распознана. Поиск: /search слова; история: /message ID. Все команды — /help.",
                main_keyboard(),
            )

    async def _callback(self, update_id: int, data: str) -> None:
        parts = data.split(":")
        action = parts[0]
        if data in {"help", "start"}:
            await self._reply(update_id, HELP, main_keyboard())
        elif data == "status":
            await self._status(update_id)
        elif action == "chats" and len(parts) == 2:
            await self._chats(update_id, self._page(parts[1]))
        elif action == "list" and len(parts) == 4 and parts[1] in {"a", "d", "c"}:
            await self._messages(update_id, parts[1], integer(parts[2]), self._page(parts[3]))
        elif action == "history" and len(parts) in {2, 3}:
            await self._history(
                update_id,
                integer(parts[1], positive=True),
                self._page(parts[2]) if len(parts) == 3 else None,
            )
        elif action == "files" and len(parts) == 3:
            await self._files(update_id, integer(parts[1], positive=True), self._page(parts[2]))
        elif action == "media" and len(parts) == 3:
            message_id, media_id = (
                integer(parts[1], positive=True),
                integer(parts[2], positive=True),
            )
            if media_id not in {row["id"] for row in await self.archive.message_media(message_id)}:
                await self._reply(update_id, "Файл больше не существует в архиве.")
            else:
                await self.archive.enqueue(
                    "notification",
                    f"ui:{update_id}:file",
                    {"media_id": media_id, "message_id": message_id, "archive_sensitive": True},
                )
        elif action == "settings" and len(parts) == 2:
            await self._settings(update_id, None if parts[1] == "g" else integer(parts[1]))
        elif (
            action == "set"
            and len(parts) == 4
            and parts[1] in {"notify_edits", "notify_deletes", "muted"}
            and parts[2] in {"0", "1"}
        ):
            chat_id = None if parts[3] == "g" else integer(parts[3])
            await self.archive.set_setting(parts[1], parts[2] == "1", chat_id)
            await self._settings(update_id, chat_id)
        elif action == "search" and len(parts) == 3 and re.fullmatch(r"[0-9a-f]{16}", parts[1]):
            await self._search(update_id, parts[1], self._page(parts[2]))
        elif action == "bookmark" and len(parts) == 3 and parts[2] in {"0", "1"}:
            await self._bookmark(update_id, integer(parts[1], positive=True), parts[2] == "1")
        elif action == "bookmarks" and len(parts) == 2:
            await self._bookmarks(update_id, self._page(parts[1]))
        elif action == "export" and len(parts) == 2:
            await self._export(
                update_id,
                message_id=None if parts[1] == "all" else integer(parts[1], positive=True),
            )
        elif action == "delete" and len(parts) == 2:
            await self._delete_request(update_id, integer(parts[1], positive=True))
        elif action == "erase" and len(parts) == 2:
            await self._delete_confirm(update_id, parts[1])
        else:
            raise ValueError("invalid_callback")

    @staticmethod
    def _page(value: str) -> int:
        page = integer(value)
        if not 0 <= page <= 1_000_000:
            raise ValueError("invalid_page")
        return page

    @staticmethod
    def _navigation(prefix: str, page: int, more: bool) -> list[dict]:
        result = []
        if page:
            result.append(button("← Назад", f"{prefix}:{page - 1}"))
        if more:
            result.append(button("Далее →", f"{prefix}:{page + 1}"))
        return result

    async def _status(self, update_id: int) -> None:
        stats = await self.archive.stats()
        names = {
            "messages": "Записи",
            "versions": "Версии",
            "deleted": "Удалённые в Telegram",
            "media": "Вложения",
            "pending_media": "Файлы в очереди",
            "inbox_pending": "Входящие в очереди",
            "jobs_pending": "Задания в очереди",
            "jobs_failed": "Ошибки заданий",
            "jobs_blocked": "Заблокированные отправки",
            "jobs_uncertain": "Отправки с неопределённым исходом",
            "jobs_attention": "Задания, требующие внимания",
            "media_attention": "Файлы, требующие внимания",
        }
        text = "Состояние архива\n" + "\n".join(
            f"{label}: {stats[key]}" for key, label in names.items() if key in stats
        )
        last = await self.archive.get_setting("last_backup_at")
        text += "\nПоследняя успешная копия: " + (
            timestamp(last, self.settings.timezone) if last else "ещё нет"
        )
        for key, label in (
            ("backup_error", "Ошибка копии"),
            ("maintenance_error", "Ошибка обслуживания"),
            ("notification_status", "Доставка"),
        ):
            value = await self.archive.get_setting(key)
            if value:
                text += f"\n{label}: {value}"
        await self._reply(update_id, text, main_keyboard())

    async def _chats(self, update_id: int, page: int) -> None:
        rows = await self.archive.list_chats(limit=PAGE + 1, offset=page * PAGE)
        text, buttons = f"Чаты · страница {page + 1}\n", []
        for row in rows[:PAGE]:
            text += f"\n{preview(row.get('title') or str(row['chat_id']), 90)} · {row['chat_id']} · записей {row['messages']}"
            buttons.append([button(f"Чат {row['chat_id']}", f"list:c:{row['chat_id']}:0")])
        if not rows:
            text += "\nЧатов пока нет. Проверьте подключение и выбранные чаты в Секретарском режиме. Старые сообщения не загружаются автоматически."
        if nav := self._navigation("chats", page, len(rows) > PAGE):
            buttons.append(nav)
        buttons.append([button("Меню", "start")])
        await self._reply(update_id, text, keyboard(buttons), sensitive=True)

    async def _messages(self, update_id: int, mode: str, chat_id: int, page: int) -> None:
        rows = await self.archive.list_messages(
            limit=PAGE + 1,
            offset=page * PAGE,
            chat_id=chat_id if mode == "c" else None,
            deleted_only=mode == "d",
        )
        label = "Удалённые" if mode == "d" else f"Чат {chat_id}" if mode == "c" else "Архив"
        text, buttons = f"{label} · страница {page + 1}\n", []
        for row in rows[:PAGE]:
            text += (
                f"\n#{row['id']} · {preview(row.get('chat_title') or str(row['chat_id']), 55)}"
                + (" · удалено" if row.get("deleted_at") is not None else "")
            )
            text += (
                "\n"
                + preview(
                    row.get("text") or row.get("caption") or "[Нет текста; откройте историю]", 180
                )
                + "\n"
            )
            buttons.append([button(f"История #{row['id']}", f"history:{row['id']}")])
        if not rows:
            text += "\nЗаписей на этой странице нет."
        if nav := self._navigation(f"list:{mode}:{chat_id}", page, len(rows) > PAGE):
            buttons.append(nav)
        if mode == "c":
            buttons.append([button("Настройки чата", f"settings:{chat_id}")])
        buttons.append([button("Меню", "start")])
        await self._reply(update_id, text, keyboard(buttons), sensitive=True)

    async def _history(self, update_id: int, message_id: int, index: int | None = None) -> None:
        history = await self.archive.history(message_id)
        if history is None:
            await self._reply(update_id, "Запись не найдена или была стёрта локально.")
            return
        index = len(history["versions"]) - 1 if index is None else index
        if history["versions"] and not 0 <= index < len(history["versions"]):
            raise ValueError("Версия не найдена.")
        marked = await self.archive.get_setting("bookmark", False, message_id_scope(message_id))
        rows = []
        if history["versions"]:
            nav = []
            if index:
                nav.append(button("← Предыдущая версия", f"history:{message_id}:{index - 1}"))
            if index + 1 < len(history["versions"]):
                nav.append(button("Следующая версия →", f"history:{message_id}:{index + 1}"))
            if nav:
                rows.append(nav)
        if history["media"]:
            rows.append([button(f"Файлы ({len(history['media'])})", f"files:{message_id}:0")])
        rows.extend(
            [
                [button("Экспорт записи", f"export:{message_id}")],
                [
                    button(
                        "Убрать закладку" if marked else "В закладки",
                        f"bookmark:{message_id}:{0 if marked else 1}",
                    )
                ],
                [button("Стереть локально…", f"delete:{message_id}"), button("Меню", "start")],
            ]
        )
        await self._reply(
            update_id,
            history_text(history, index, self.settings.timezone),
            keyboard(rows),
            message_id=message_id,
            sensitive=True,
        )

    async def _files(self, update_id: int, message_id: int, page: int) -> None:
        history = await self.archive.history(message_id)
        if not history:
            await self._reply(update_id, "Запись больше не существует.")
            return
        media = history["media"]
        current = media[page * PAGE : (page + 1) * PAGE]
        text = f"Вложения записи #{message_id} · страница {page + 1}\n\n" + (
            "\n".join(media_status(row) for row in current) or "На этой странице файлов нет."
        )
        rows = [
            [button(f"Получить файл #{row['id']}", f"media:{message_id}:{row['id']}")]
            for row in current
        ]
        if nav := self._navigation(f"files:{message_id}", page, len(media) > (page + 1) * PAGE):
            rows.append(nav)
        rows.append([button("История", f"history:{message_id}")])
        await self._reply(
            update_id,
            text
            + "\n\nНедоступные файлы нельзя восстановить из одного file_id. /retry повторяет безопасные ошибки загрузки.",
            keyboard(rows),
            message_id=message_id,
            sensitive=True,
        )

    async def _settings(self, update_id: int, chat_id: int | None) -> None:
        scope = "g" if chat_id is None else str(chat_id)
        text = (
            "Настройки уведомлений"
            + (f" чата {chat_id}" if chat_id is not None else " глобально")
            + "\nАрхивирование всех доступных сообщений продолжается.\n"
        )
        rows = []
        for key, title, default in (
            ("notify_edits", "Редакции", True),
            ("notify_deletes", "Удаления", True),
            ("muted", "Без уведомлений", False),
        ):
            value = await self.archive.get_setting(key, default, chat_id)
            text += f"\n{title}: {'да' if value else 'нет'}"
            rows.append(
                [
                    button(f"{title}: вкл", f"set:{key}:1:{scope}"),
                    button("выкл", f"set:{key}:0:{scope}"),
                ]
            )
        text += f"\n\nПорог правок: {await self.archive.get_setting('ignore_threshold', 0)}% (/ignore N)"
        text += f"\nСводка: {await self.archive.get_setting('digest_minutes', 0)} мин (/digest N)"
        quiet = await self.archive.get_setting("quiet")
        text += "\nТихие часы: " + (
            f"{quiet['start']}–{quiet['end']} {quiet['timezone']}" if quiet else "нет"
        )
        days = await self.archive.get_setting("retention_days", 0)
        text += f"\nХранение: {str(days) + ' дней' if days else 'бессрочно'} (/retention N)"
        if chat_id is not None:
            text += "\nГлобальное отключение уведомлений действует на все чаты. Порог, сводка и тихие часы задаются глобально."
        rows.append([button("Меню", "start")])
        await self._reply(update_id, text, keyboard(rows))

    async def _search(self, update_id: int, token: str, page: int) -> None:
        state = await self.archive.get_setting(f"ui_search:{token}")
        if not state or state["expires"] < time.time():
            await self._reply(update_id, "Поиск устарел. Повторите /search слова.")
            return
        rows = await self.archive.search(
            state["query"], limit=PAGE + 1, offset=page * PAGE, chat_id=state["chat_id"]
        )
        text, buttons = (
            f"Поиск: {preview(state['query'], 120)} · страница {page + 1}\nРезультаты по всем сохранённым версиям.\n",
            [],
        )
        for row in rows[:PAGE]:
            text += f"\n#{row['message_id']} · версия {row['version_id']} · чат {row['chat_id']}\n{preview(row.get('text') or row.get('caption'), 180)}\n"
            buttons.append(
                [button(f"История #{row['message_id']}", f"history:{row['message_id']}")]
            )
        if not rows:
            text += "\nСовпадений нет."
        if nav := self._navigation(f"search:{token}", page, len(rows) > PAGE):
            buttons.append(nav)
        await self._reply(
            update_id, text, keyboard(buttons or [[button("Меню", "start")]]), sensitive=True
        )

    async def _bookmark(self, update_id: int, message_id: int, desired: bool) -> None:
        if not await self.archive.set_bookmark(message_id, desired):
            await self._reply(update_id, "Запись не найдена.")
            return
        await self._reply(
            update_id,
            f"Запись #{message_id} "
            + ("добавлена в закладки." if desired else "убрана из закладок."),
            keyboard(
                [[button("История", f"history:{message_id}"), button("Закладки", "bookmarks:0")]]
            ),
        )

    async def _bookmarks(self, update_id: int, page: int) -> None:
        rows = await self.archive.list_bookmarks(limit=PAGE + 1, offset=page * PAGE)
        text, buttons = f"Закладки · страница {page + 1}\n", []
        for row in rows[:PAGE]:
            text += f"\n#{row['id']} · {preview(row.get('text') or row.get('caption') or '[Нет текста]', 180)}\n"
            buttons.append([button(f"История #{row['id']}", f"history:{row['id']}")])
        if not rows:
            text += "\nЗакладок нет. Добавьте /bookmark ID или откройте историю записи."
        if nav := self._navigation("bookmarks", page, len(rows) > PAGE):
            buttons.append(nav)
        buttons.append([button("Меню", "start")])
        await self._reply(update_id, text, keyboard(buttons), sensitive=True)

    async def _export(
        self,
        update_id: int,
        *,
        chat_id: int | None = None,
        message_id: int | None = None,
        include_media: bool = False,
    ) -> None:
        if message_id is not None and await self.archive.history(message_id) is None:
            await self._reply(update_id, "Запись не найдена.")
            return
        await self.archive.enqueue(
            "export",
            f"ui:{update_id}:export",
            {"chat_id": chat_id, "message_id": message_id, "include_media": include_media},
        )
        await self._reply(
            update_id,
            "Экспорт поставлен в очередь: полные версии JSONL + HTML"
            + (
                " и сохранённые вложения."
                if include_media
                else " без вложений. Для файлов используйте /export_media [ID_чата]."
            )
            + " Расшифрованный ZIP будет отправлен в этот личный чат, если укладывается в лимит Bot API.",
        )

    async def _delete_request(self, update_id: int, message_id: int) -> None:
        if await self.archive.history(message_id) is None:
            await self._reply(update_id, "Запись не найдена или уже стёрта.")
            return
        key = f"ui_delete_request:{update_id}"
        nonce = await self.archive.get_setting(key)
        if not nonce:
            nonce = secrets.token_hex(12)
            await self.archive.set_setting(
                f"ui_delete:{nonce}",
                {
                    "message_id": message_id,
                    "owner_id": self.settings.owner_id,
                    "expires": time.time() + 300,
                    "done": False,
                },
            )
            await self.archive.set_setting(key, nonce)
        await self._reply(
            update_id,
            f"Стереть локальную запись #{message_id}, все её версии и сохранённые файлы? Действие необратимо. Повторные события этой записи не восстановят её.\n\nСтарые резервные копии и ранее отправленные уведомления автоматически НЕ удаляются. Подтверждение действует 5 минут.",
            keyboard(
                [
                    [button("Да, стереть локально", f"erase:{nonce}")],
                    [button("Отмена: история", f"history:{message_id}")],
                ]
            ),
        )

    async def _delete_confirm(self, update_id: int, nonce: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{24}", nonce):
            raise ValueError("invalid_nonce")
        state = await self.archive.get_setting(f"ui_delete:{nonce}")
        if (
            not state
            or state["owner_id"] != self.settings.owner_id
            or state["expires"] < time.time()
        ):
            await self._reply(update_id, "Подтверждение истекло. Повторите /delete ID.")
            return
        if not state["done"]:
            await self.operations.erase(state["message_id"])
            await self.archive.set_setting(f"ui_delete:{nonce}", {**state, "done": True})
        await self._reply(
            update_id,
            f"Запись #{state['message_id']} стёрта локально. Старые резервные копии и ранее отправленные сообщения Telegram остаются.",
            main_keyboard(),
        )


def message_id_scope(message_id: int) -> str:
    return f"message:{message_id}"


def quiet_until(quiet: dict | None, now: float) -> float | None:
    """Return the next end of an active quiet period in its own ZoneInfo zone."""
    if not quiet:
        return None
    zone = ZoneInfo(quiet["timezone"])
    local = datetime.fromtimestamp(now, zone)
    start_h, start_m = map(int, quiet["start"].split(":"))
    end_h, end_m = map(int, quiet["end"].split(":"))
    minute = local.hour * 60 + local.minute
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    if start == end:
        return None
    active = start <= minute < end if start < end else minute >= start or minute < end
    if not active:
        return None
    end_date = local.date() + (
        timedelta(days=1) if start > end and minute >= start else timedelta()
    )
    return datetime(
        end_date.year, end_date.month, end_date.day, end_h, end_m, tzinfo=zone
    ).timestamp()


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _NoLease(Exception):
    pass


class NotificationWorker:
    def __init__(self, archive, api, settings, key: bytes, operations):
        self.archive, self.api, self.settings = archive, api, settings
        self.key, self.operations = key, operations
        self._last_send = 0.0
        self._send_attempted = False

    async def _paced(self) -> None:
        delay = 1 - (time.monotonic() - self._last_send)
        if delay > 0:
            await asyncio.sleep(delay)
        # Reserve before sending: failures count towards Telegram's rate limit too.
        self._last_send = time.monotonic()

    async def _text(self, text: str, markup: dict | None = None) -> None:
        await self._paced()
        options = {"reply_markup": markup} if markup else {}
        try:
            self._send_attempted = True
            await self.api.send_text(self.settings.owner_id, text, **options)
        except TelegramError as error:
            if error.code != 400:
                raise
            parser = _Plain()
            parser.feed(text)
            await self._paced()
            await self.api.call(
                "sendMessage",
                chat_id=self.settings.owner_id,
                text="".join(parser.parts),
                link_preview_options={"is_disabled": True},
                **options,
            )

    async def _notice(self, text: str, markup: dict | None = None) -> None:
        parts = escaped_parts(text)
        # Views are bounded. Fallback notices must also have one send/one job;
        # multi-part events are separately enqueued before attempting any send.
        await self._text(parts[0], markup)

    async def _heartbeat(self, jobs: list[dict]) -> None:
        while True:
            await asyncio.sleep(30)
            for job in jobs:
                if not await self.archive.renew_job(job["id"], job["attempts"], 3600):
                    return

    async def _finish(self, job: dict) -> None:
        await self.archive.finish_job(job["id"], attempts=job["attempts"])

    async def _renew(self, job: dict) -> None:
        if not await self.archive.renew_job(job["id"], job["attempts"], 3600):
            raise _NoLease

    async def _policy(self, job: dict, *, schedule: bool = True) -> tuple[dict | None, float]:
        payload = job["payload"]
        history = await self.archive.history(payload["message_id"])
        if not history:
            return None, 0
        chat_id = history["message"]["chat_id"]
        event = payload["event"]
        if (
            await self.archive.get_setting("muted", False)
            or await self.archive.get_setting("muted", False, chat_id)
            or not await self.archive.get_setting(
                "notify_edits" if event == "edit" else "notify_deletes", True, chat_id
            )
        ):
            return None, 0
        if event == "edit":
            versions = history["versions"]
            index = next(
                (
                    i
                    for i, version in enumerate(versions)
                    if version["id"] == payload.get("version_id")
                ),
                None,
            )
            if index is None:
                return None, 0
            threshold = await self.archive.get_setting("ignore_threshold", 0)
            if not edit_visible(versions[index - 1] if index else None, versions[index], threshold):
                return None, 0
        now = time.time()
        due = quiet_until(await self.archive.get_setting("quiet"), now) or 0
        digest = await self.archive.get_setting("digest_minutes", 0)
        if digest and schedule:
            state_key = f"notification_due:{job['id']}"
            state = await self.archive.get_setting(state_key)
            if not state or state["minutes"] != digest:
                state = {"minutes": digest, "due": (int(now // (digest * 60)) + 1) * digest * 60}
                await self.archive.set_setting(state_key, state)
            due = max(due, state["due"])
        return history, due

    async def _defer(self, job: dict, due: float) -> None:
        await self.archive.retry_job(
            job["id"], "scheduled", max(0.05, due - time.time()), attempts=job["attempts"]
        )

    async def _event(self, job: dict, jobs: list[dict]) -> None:
        history, due = await self._policy(job)
        if history is None:
            await self._finish(job)
            return
        if due > time.time():
            await self._defer(job, due)
            return
        digest = await self.archive.get_setting("digest_minutes", 0)
        if digest:
            extra = await self.archive.claim_notification_events(limit=19, lease_seconds=3600)
            jobs.extend(extra)
            ready = []
            for item in jobs:
                record, item_due = await self._policy(item)
                if record is None:
                    await self._finish(item)
                elif item_due > time.time():
                    await self._defer(item, item_due)
                else:
                    ready.append((item, record))
            if not ready:
                return
            async with self.operations.lock:
                final = []
                for item, _ in ready:
                    await self._renew(item)
                    record, item_due = await self._policy(item)
                    if record is None:
                        await self._finish(item)
                    elif item_due > time.time():
                        await self._defer(item, item_due)
                    else:
                        final.append((item, record))
                if not final:
                    return
                edits = sum(item["payload"]["event"] == "edit" for item, _ in final)
                text = f"Сводка событий: {len(final)}\nИзменения: {edits}; удаления в Telegram: {len(final) - edits}.\nTelegram не сообщает, кто удалил сообщение.\n"
                rows = []
                for item, record in final:
                    message_id = record["message"]["id"]
                    label = "изменено" if item["payload"]["event"] == "edit" else "удалено"
                    text += f"\n#{message_id} · {label} · чат {record['message']['chat_id']}"
                    rows.append(
                        [button(f"История #{message_id}: {label}", f"history:{message_id}")]
                    )
                await self._notice(text, keyboard(rows))
                for item, _ in final:
                    await self._finish(item)
            return
        async with self.operations.lock:
            await self._renew(job)
            history, due = await self._policy(job)
            if history is None:
                await self._finish(job)
                return
            if due > time.time():
                await self._defer(job, due)
                return
            payload = job["payload"]
            text = event_text(
                history, payload["event"], payload.get("version_id"), self.settings.timezone
            )
            # A preview can expand after escaping. Separate jobs before sending so
            # a later part never causes the successful first part to be replayed.
            parts = escaped_parts(text)
            markup = keyboard([[button("История и файлы", f"history:{payload['message_id']}")]])
            if len(parts) > 1:
                for index, part in enumerate(parts):
                    await self.archive.enqueue(
                        "notification",
                        f"event-part:{job['id']}:{index}",
                        {
                            "text": part,
                            "reply_markup": markup,
                            "message_id": payload["message_id"],
                            "event_part": True,
                            "event_context": payload,
                            "archive_sensitive": True,
                        },
                    )
            else:
                await self._text(parts[0], markup)
            await self._finish(job)

    async def _document(self, job: dict) -> None:
        payload = job["payload"]
        async with self.operations.lock:
            await self._renew(job)
            if "media_id" in payload:
                media = await self.archive.media(payload["media_id"])
                if not media or media["state"] != "saved" or not media.get("object_path"):
                    await self._notice(
                        "Файл пока не сохранён или недоступен. Проверьте историю; /retry повторяет безопасные ошибки загрузки."
                    )
                    return
                history = await self.archive.history(media["message_fk"])
                if not history:
                    return
                source = next(
                    (
                        version
                        for version in history["versions"]
                        if version["id"] == media["version_id"]
                    ),
                    None,
                )
                if source is None:
                    return
                if source["payload"].get("has_protected_content"):
                    await self._notice(
                        "Это вложение сообщения с защитой содержимого Telegram. Оно остаётся сохранённым в локальном архиве; повторная отправка через бота отключена."
                    )
                    return
                path = object_path(self.settings.data_dir, media["object_path"])
                name = str(
                    ((media.get("metadata") or {}).get("file") or {}).get("file_name")
                    or f"media-{media['id']}.bin"
                )
                filename = Path(name.replace("\\", "/")).name or f"media-{media['id']}.bin"
                filename = (
                    "".join(
                        character
                        for character in filename
                        if ord(character) >= 32 and ord(character) != 127
                    )[:200]
                    or f"media-{media['id']}.bin"
                )
                caption = f"Сохранённое вложение #{media['id']}"
            else:
                root = (self.settings.data_dir / "exports").resolve()
                relative = payload.get("export_path")
                if not isinstance(relative, str):
                    raise ValueError("invalid_export_path")
                path = (self.settings.data_dir / relative).resolve()
                if root not in path.parents or path.suffix != ".sba":
                    raise ValueError("invalid_export_path")
                filename, caption = "telegram-archive.zip", "Экспорт личного архива: JSONL + HTML"
            if not path.is_file():
                await self._notice(
                    "Локальный файл недоступен или экспорт устарел после обслуживания/стирания. Повторите экспорт или проверьте /status."
                )
                return
            digest, size = await filesystem_call(verify_object, path, self.key)
            if "media_id" in payload and (
                (media.get("sha256") and media["sha256"] != digest)
                or (media.get("size") is not None and media["size"] != size)
            ):
                raise ValueError("object_metadata_mismatch")
            local = urlparse(self.settings.api_url).hostname != "api.telegram.org"
            limit = 2_000_000_000 if local else 50_000_000
            if size > limit:
                await self._notice(
                    f"Файл сохранён локально, но превышает лимит отправки {'локального' if local else 'облачного'} Bot API ({limit // 1_000_000} МБ). Используйте локальный экспорт или меньший объём."
                )
                return
            await self._renew(job)
            await self._paced()
            self._send_attempted = True
            await self.api.send_document(
                self.settings.owner_id, decrypted_stream(path, self.key), filename, caption
            )

    async def run_once(self) -> bool:
        job = await self.archive.claim_job("notification", lease_seconds=3600)
        if not job:
            return False
        jobs = [job]
        self._send_attempted = False
        pulse = asyncio.create_task(self._heartbeat(jobs))
        try:
            payload = job["payload"]
            await self._renew(job)
            if "answer_callback_query" in payload:
                self._send_attempted = True
                await self.api.call(
                    "answerCallbackQuery", callback_query_id=payload["answer_callback_query"]
                )
                await self._finish(job)
            elif payload.get("event") in {"edit", "delete"}:
                await self._event(job, jobs)
            elif "media_id" in payload or "export_path" in payload:
                await self._document(job)
                await self._finish(job)
            elif isinstance(payload.get("text"), str):
                async with self.operations.lock:
                    await self._renew(job)
                    # Erasure removes event-part jobs too; the lease check above
                    # fences stale workers, the last history check fences deletion.
                    if payload.get("event_part"):
                        record, due = await self._policy(
                            {**job, "payload": payload["event_context"]}, schedule=False
                        )
                        if record is None:
                            await self._finish(job)
                            return True
                        if due > time.time():
                            await self._defer(job, due)
                            return True
                    if "message_id" not in payload or await self.archive.history(
                        payload["message_id"]
                    ):
                        await self._text(payload["text"], payload.get("reply_markup"))
                    await self._finish(job)
            else:
                raise ValueError("invalid_notification_payload")
        except asyncio.CancelledError:
            # No retry: the lease expires into uncertain, never automatic resend.
            raise
        except _NoLease:
            pass
        except TelegramError as error:
            state = (
                "blocked"
                if error.code == 403
                else "uncertain"
                if error.code == 0
                else "pending"
                if error.code == 429 or error.code >= 500
                else "failed"
            )
            code = f"notify:{error.category}:{error.code}"
            delay = max(error.retry_after, min(2 ** min(job["attempts"], 9), 600))
            for item in jobs:
                await self.archive.retry_job(
                    item["id"], code, delay, state=state, attempts=item["attempts"]
                )
            status = {
                "blocked": "Бот заблокирован или доступ запрещён. Разблокируйте и используйте /retry.",
                "uncertain": "Исход отправки неизвестен: Telegram мог принять её. Автоповтор отключён; /retry_uncertain разрешает повтор с риском дубликата.",
                "pending": "Временная ошибка Telegram; повтор по расписанию.",
                "failed": "Telegram отклонил отправку. Проверьте настройки и используйте /retry.",
            }[state]
            await self.archive.set_setting("notification_status", status)
            logger.warning(
                "notification_result category=%s code=%d state=%s",
                error.category,
                error.code,
                state,
            )
        except Exception as error:
            # Local errors occur before send except unexpected API adapter errors;
            # conservative uncertain avoids repeating a possibly accepted upload.
            code = type(error).__name__
            state = "uncertain" if self._send_attempted else "failed"
            for item in jobs:
                await self.archive.retry_job(
                    item["id"], code, 60, state=state, attempts=item["attempts"]
                )
            await self.archive.set_setting(
                "notification_status",
                "Ошибка локального файла/доставки. Архив сохранён; проверьте /status и /retry. Неопределённые отправки требуют /retry_uncertain.",
            )
            logger.error("notification_error type=%s state=%s", code, state)
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        return True

    async def run(self) -> None:
        while True:
            if not await self.run_once():
                await asyncio.sleep(0.25)
