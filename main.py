from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import os
import asyncio
from datetime import datetime
import sqlite3

from database import Database
from utils import (
    escape_markdown,
    format_as_quote,
    save_message,
    collect_media_from_message,
    send_media_message
)

load_dotenv()

bot = Bot(token=os.getenv("TOKEN"))
dp = Dispatcher()

db = Database()

MESSAGES_LIFETIME = int(os.getenv("MESSAGES_LIFETIME", 24))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", 3600))

def get_status_message():
    settings = db.get_settings()
    stats = db.get_stats()
    
    ignore_status = "no limit" if settings["ignore_changes_below"] == 0 else f"*{settings['ignore_changes_below']}* chars"
    
    status_text = (
        "*Bot Status:*\n\n"
        f"Messages saved: *{stats['total_messages']}*\n"
        f"Media files saved: *{stats['total_media']}*\n"
        f"Ignore edits below: {ignore_status}\n\n"
        "*Settings:*"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"Edited: {'ON' if settings['notify_edited'] else 'OFF'}", 
        callback_data="toggle_edited"
    )
    builder.button(
        text=f"Deleted: {'ON' if settings['notify_deleted'] else 'OFF'}", 
        callback_data="toggle_deleted"
    )
    builder.button(
        text=f"Scheduled: {'ON' if settings.get('notify_scheduled', True) else 'OFF'}", 
        callback_data="toggle_scheduled"
    )
    builder.adjust(1)
    
    return status_text, builder.as_markup()

@dp.message()
async def start_command(message: types.Message):
    if str(message.from_user.id) != os.getenv("USER_ID"):
        return
        
    if not message.from_user.is_premium:
        await message.answer("_Telegram Premium is required to use this bot_", parse_mode="MarkdownV2")
        return
        
    if message.text and message.text.startswith("/") and message.text[1:].isdigit():
        message_id = int(message.text[1:])
        
        history = db.get_message_history(message_id)
        
        if not history:
            await message.answer(f"Message /{message_id} not found", parse_mode="MarkdownV2")
            return
            
        text = f"📝 Message history /{message_id} from @{escape_markdown(history['username'])}:\n\n"
        
        if history['is_forwarded'] and history['forward_from']:
            text += f"_Forwarded from @{escape_markdown(history['forward_from'])}_\n\n"
            
        dt = datetime.fromisoformat(history['date'])
        formatted_time = dt.strftime("%H:%M:%S")
        
        if history['is_scheduled']:
            text += f"created _{formatted_time}_ ⏱ _scheduled message_\n{format_as_quote(history['original_text'])}\n\n"
        else:
            text += f"created _{formatted_time}_\n{format_as_quote(history['original_text'])}\n\n"
        
        for action_type, old_text, new_text, action_date in history['actions']:
            dt = datetime.fromisoformat(action_date)
            formatted_time = dt.strftime("%H:%M:%S")
            
            if action_type == 'edit':
                text += f"✏️ _{formatted_time}_\n{format_as_quote(new_text)}\n\n"
            elif action_type == 'delete':
                text += f"🗑 _{formatted_time}_\n{format_as_quote(old_text)}\n\n"
        
        await send_media_message(bot, history['media_files'], text)
        return
        
    if message.text == "/start":
        start_text = (
            "*Telegram Spy Bot*\n\n"
            "To use this bot you need to:\n"
            "1\\. Enable *Business Mode* for this bot in @BotFather\n"
            "2\\. Connect bot to *Telegram Business* in app settings\n\n"
            "_Available commands:_ /help\n\n"
            "Recommended settings:\n"
            "\\- Set minimum edit size to notify about: `/ignore 3`\n\n"
        )
        await message.answer(start_text, parse_mode="MarkdownV2")
    elif message.text == "/help":
        help_text = (
            "*Available commands:*\n\n"
            "/help \\- show this message\n"
            "/bot \\- show bot statistics\n"
            "/user \\[username\\] or /u \\- show user statistics\n"
            "/history \\[username\\] \\[limit\\] or /h \\- show user action history\n"
            "/cleanup or /c \\- clear data\n"
            "/ignore \\[amount\\] \\- ignore edits with less than N changed characters\n\n"
        )
        await message.answer(help_text, parse_mode="MarkdownV2")
    elif message.text == "/cleanup" or message.text == "/c":
        await message.answer(
            "Usage:\n`/cleanup all` or `/c all` to clear entire database\n`/cleanup username` or `/c username` to delete user data", 
            parse_mode="MarkdownV2"
        )
    elif message.text == "/cleanup all" or message.text == "/c all":
        deleted_messages, deleted_files = db.cleanup_all()
        await message.answer(
            f"🗑 *Database completely cleared*\n\n"
            f"Messages deleted: *{deleted_messages}*\n"
            f"Files deleted: *{deleted_files}*",
            parse_mode="MarkdownV2"
        )
    elif message.text.startswith("/cleanup ") or message.text.startswith("/c "):
        username = message.text.split()[1].lstrip("@")
        
        if username == "all":
            deleted_messages, deleted_files = db.cleanup_all()
            await message.answer(
                f"🗑 *Database completely cleared*\n\n"
                f"Messages deleted: *{deleted_messages}*\n"
                f"Files deleted: *{deleted_files}*",
                parse_mode="MarkdownV2"
            )
            return
            
        deleted_messages, deleted_files = db.cleanup_user_data(username)
        
        if deleted_messages == 0:
            await message.answer(f"User @{escape_markdown(username)} not found", parse_mode="MarkdownV2")
            return
            
        await message.answer(
            f"🗑 *Data for @{escape_markdown(username)} deleted*\n\n"
            f"Messages deleted: *{deleted_messages}*\n"
            f"Files deleted: *{deleted_files}*",
            parse_mode="MarkdownV2"
        )
    elif message.text.startswith("/history") or message.text == "/h":
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Usage: `/history username [limit]` or `/h username [limit]`", parse_mode="MarkdownV2")
            return
            
        username = parts[1].lstrip("@")
        limit = 5
        if len(parts) > 2:
            try:
                limit = int(parts[2])
                if limit < 1:
                    raise ValueError
            except ValueError:
                await message.answer("Limit must be a positive number", parse_mode="MarkdownV2")
                return

        user_id, actions = db.get_user_actions(username, limit)
        
        if not actions:
            await message.answer(f"No actions found for @{escape_markdown(username)}", parse_mode="MarkdownV2")
            return
            
        if user_id:
            text = f"📋 Actions by @{escape_markdown(username)} \\(ID: `{user_id}`\\):\n\n"
        else:
            text = f"📋 Actions by @{escape_markdown(username)}:\n\n"
        
        for action_name, msg_text, date, is_forwarded, forward_from, chat_id, message_id in actions:
            dt = datetime.fromisoformat(date)
            time = dt.strftime("%H:%M:%S")
            
            if action_name == 'deleted':
                icon = "🗑"
            else:
                icon = "✏️"
            
            if is_forwarded and forward_from:
                text += f"{icon} _{escape_markdown(time)}_ /{message_id} \\(_{escape_markdown(f'from @{forward_from}')}_)\n"
            else:
                text += f"{icon} _{escape_markdown(time)}_ /{message_id}\n"
            text += f"{format_as_quote(msg_text)}\n\n"
        
        await message.answer(text, parse_mode="MarkdownV2")
    elif message.text == "/bot":
        status_text, reply_markup = get_status_message()
        
        await message.answer(
            status_text, 
            parse_mode="MarkdownV2",
            reply_markup=reply_markup
        )
    elif message.text == "/user" or message.text == "/u":
        await message.answer(
            "Usage: `/user username` or `/u username` to show user statistics",
            parse_mode="MarkdownV2"
        )
    elif message.text.startswith("/user ") or message.text.startswith("/u "):
        username = message.text.split()[1].lstrip("@")
        stats = db.get_user_stats(username)
        
        if not stats:
            await message.answer(f"User @{escape_markdown(username)} not found", parse_mode="MarkdownV2")
            return
            
        text = (
            f"📊 *Stats for @{escape_markdown(username)}*\n\n"
            f"Messages: *{stats['total_messages']}*\n"
            f"Media files: *{stats['total_media']}*\n"
            f"Actions: *{stats['total_actions']}*\n\n"
            f"_Last actions: `/h {username} 5`_"
        )
        
        builder = InlineKeyboardBuilder()
        builder.button(
            text=f"Notify: {'ON' if stats['notify_enabled'] else 'OFF'}", 
            callback_data=f"toggle_notify_{stats['user_id']}"
        )
        
        await message.answer(
            text,
            parse_mode="MarkdownV2",
            reply_markup=builder.as_markup()
        )
    elif message.text == "/ignore":
        settings = db.get_settings()
        current = settings["ignore_changes_below"]
        status = "no limit" if current == 0 else f"*{current}* characters"
        
        await message.answer(
            f"🔍 *Ignore small edits*\n\n"
            f"Current setting: {status} characters\n\n"
            f"Usage:\n"
            f"`/ignore 0` \\- show all edits \\(no limit\\)\n"
            f"`/ignore 5` \\- ignore edits with less than 5 changed characters",
            parse_mode="MarkdownV2"
        )
    elif message.text.startswith("/ignore "):
        try:
            amount = int(message.text.split()[1])
            if amount < 0:
                raise ValueError
            db.set_ignore_changes_below(amount)
            await message.answer(
                f"Now ignoring edits with less than *{amount}* changed characters",
                parse_mode="MarkdownV2"
            )
        except (IndexError, ValueError):
            await message.answer(
                "Please specify a valid number",
                parse_mode="MarkdownV2"
            )
    else:
        await message.answer(
            "Unknown command\\. Use /help to see available commands\\.",
            parse_mode="MarkdownV2"
        )

@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    if str(callback.from_user.id) != os.getenv("USER_ID"):
        return
    
    action = callback.data
    if action == "toggle_edited":
        db.toggle_setting("notify_edited")
    elif action == "toggle_deleted":
        db.toggle_setting("notify_deleted")
    elif action == "toggle_scheduled":
        db.toggle_setting("notify_scheduled")
    elif action.startswith("toggle_notify_"):
        user_id = int(action.split("_")[2])
        db.toggle_user_notify(user_id)
        
        cursor = sqlite3.connect(db.db_path).cursor()
        cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
        username = cursor.fetchone()[0]
        cursor.close()
        
        stats = db.get_user_stats(username)
        if not stats:
            await callback.answer("User not found")
            return
            
        text = (
            f"📊 *Stats for @{escape_markdown(username)}*\n\n"
            f"Messages: *{stats['total_messages']}*\n"
            f"Media files: *{stats['total_media']}*\n"
            f"Actions: *{stats['total_actions']}*\n\n"
            f"_Last actions: `/h {username} 5`_"
        )
        
        builder = InlineKeyboardBuilder()
        builder.button(
            text=f"Notify: {'ON' if stats['notify_enabled'] else 'OFF'}", 
            callback_data=f"toggle_notify_{stats['user_id']}"
        )
        
        await callback.message.edit_text(
            text,
            parse_mode="MarkdownV2",
            reply_markup=builder.as_markup()
        )
        await callback.answer()
        return
    elif action.startswith("history_"):
        _, chat_id, message_id = action.split("_")
        
        current_message = db.get_message(int(chat_id), int(message_id))
        if not current_message:
            await callback.answer("Message not found")
            return
            
        if current_message['media_files']:
            await callback.answer("История изменений недоступна для медиасообщений")
            return
            
        cursor = sqlite3.connect(db.db_path).cursor()
        cursor.execute("""
            SELECT old_text, new_text, action_date
            FROM message_actions
            WHERE chat_id = ? AND message_id = ? AND action_type = 'edit'
            ORDER BY action_date ASC
        """, (chat_id, message_id))
        history = cursor.fetchall()
        cursor.close()
        
        msg_id = f"/{message_id}"
            
        text = f"📝 Edit History {msg_id}:\n\n"
        
        for i, (old_text, edited_at) in enumerate(history):
            dt = datetime.fromisoformat(edited_at)
            formatted_time = dt.strftime("%H:%M:%S")
            text += f"_{formatted_time}_\n{format_as_quote(old_text)}\n↓\n"
            
        current_time = datetime.now().strftime("%H:%M:%S")
        text += f"_{current_time}_\n{format_as_quote(current_message['text'])}"
        
        await callback.message.edit_text(
            text,
            parse_mode="MarkdownV2",
            reply_markup=None
        )
        await callback.answer()
        return
    
    status_text, reply_markup = get_status_message()
    
    await callback.message.edit_text(
        status_text,
        parse_mode="MarkdownV2",
        reply_markup=reply_markup
    )
    await callback.answer()

@dp.business_message()
async def message(message: types.Message):
    if str(message.from_user.id) != os.getenv("USER_ID"):
        if message.date.second == 1:
            settings = db.get_settings()
            if settings.get("notify_scheduled", True):
                msg_id = f"/{message.message_id}"
                text = f"⏱ Possibly @{escape_markdown(message.from_user.username or str(message.from_user.id))} sent a scheduled message:\n\n"
                
                message_text = message.md_text or message.caption or ""
                text += f"{format_as_quote(message_text)}\n\n{msg_id}"
                
                media_files = await collect_media_from_message(bot, message)
                await send_media_message(bot, media_files, text)
                
        await save_message(bot, message, db)

@dp.edited_business_message()
async def edited_message(message: types.Message):
    if str(message.from_user.id) == os.getenv("USER_ID"):
        return

    settings = db.get_settings()
    if not settings["notify_edited"]:
        return

    old_message = db.get_message(message.chat.id, message.message_id)
        
    if not old_message:
        return

    cursor = sqlite3.connect(db.db_path).cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (f"notify_user_{old_message['user_id']}",))
    row = cursor.fetchone()
    notify_enabled = bool(row[0]) if row else True
    cursor.close()

    if not notify_enabled:
        return

    new_text = message.md_text or message.caption or ""
    
    if settings["ignore_changes_below"] > 0:
        old_text = old_message['text']
        min_len = min(len(old_text), len(new_text))
        max_len = max(len(old_text), len(new_text))

        changes = sum(1 for i in range(min_len) if old_text[i] != new_text[i])
        changes += max_len - min_len
        
        if changes < settings["ignore_changes_below"]:
            return
    
    db.save_message_action(message.chat.id, message.message_id, 'edit', old_message['text'], new_text)
    
    media_files = old_message['media_files']
    
    msg_id = f"/{message.message_id}"
    
    text = (
        f"✏️ @{old_message['username']} edited message:\n"
        f"\n{format_as_quote(old_message['text'])}\n↓"
        f"\n{format_as_quote(new_text)}\n\n"
        f"{msg_id}"
    )
    
    await send_media_message(bot, media_files, text)
    
    await save_message(bot, message, db)

@dp.deleted_business_messages()
async def deleted_message(business_messages: types.BusinessMessagesDeleted):
    settings = db.get_settings()
    if not settings["notify_deleted"]:
        return

    for message_id in business_messages.message_ids:
        old_message = db.get_message(business_messages.chat.id, message_id)
        
        if not old_message or str(old_message['user_id']) == os.getenv("USER_ID"):
            continue

        cursor = sqlite3.connect(db.db_path).cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (f"notify_user_{old_message['user_id']}",))
        row = cursor.fetchone()
        notify_enabled = bool(row[0]) if row else True
        cursor.close()

        if not notify_enabled:
            continue
            
        msg_id = f"/{message_id}"

        text = f"🗑 @{escape_markdown(old_message['username'])} deleted message:\n\n"
        
        if old_message['is_forwarded'] and old_message['forward_from']:
            text += f"_Forwarded from @{escape_markdown(old_message['forward_from'])}_\n"
            
        text += f"{format_as_quote(old_message['text'])}\n\n{msg_id}"
        
        await send_media_message(bot, old_message['media_files'], text)
            
        db.delete_message(business_messages.chat.id, message_id)

@dp.business_connection()
async def on_business_connection(event: types.BusinessConnection):
    if str(event.user.id) != os.getenv("USER_ID"):
        return

    if event.is_enabled:
        text_="_Bot successfully disconnected from Telegram Business._"
    else:
        text_="_Bot successfully connected to Telegram Business._"
    
    await bot.send_message(
        chat_id=event.user.id,
        text=text_,
        parse_mode="MarkdownV2"
    )

async def cleanup_messages():
    while True:
        deleted_count = db.cleanup_old_messages(hours=MESSAGES_LIFETIME)
        print(f"{datetime.now()}: Deleted {deleted_count} outdated messages")
        await asyncio.sleep(CLEANUP_INTERVAL)

async def main():
    asyncio.create_task(cleanup_messages())
    
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
            "business_connection",
            "callback_query"
        ],
    )

if __name__ == "__main__":
    print("Starting bot...")
    asyncio.run(main())