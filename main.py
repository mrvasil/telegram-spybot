from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import os
import asyncio
from datetime import datetime, timedelta
import re

from database import Database
from media import download_media

load_dotenv()

bot = Bot(token=os.getenv("TOKEN"))
dp = Dispatcher()

db = Database()

MESSAGES_LIFETIME = int(os.getenv("MESSAGES_LIFETIME", 24)) #часы
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", 3600)) #секунды

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    characters = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    return re.sub(f"([{''.join(map(re.escape, characters))}])", r'\\\1', text)

def format_as_quote(text: str) -> str:
    if not text:
        return ""
    lines = text.split('\n')
    return '\n'.join(f'> {line}' if line.strip() else '>' for line in lines)

async def save_message(message: types.Message):
    if str(message.from_user.id) == os.getenv("USER_ID"):
        return
        
    saved_media = await db.save_message(message)
    
    for media_path, file_id in saved_media:
        await download_media(bot, file_id, media_path)

@dp.message()
async def start_command(message: types.Message):
    if str(message.from_user.id) != os.getenv("USER_ID"):
        return
        
    if not message.from_user.is_premium:
        await message.answer("_Для использования бота необходим Telegram Premium_", parse_mode="MarkdownV2")
        return
        
    if message.text == "/start":
        await message.answer(
            "Напишите любое сообщение, чтобы посмотреть статус бота"
        )
    else:
        stats = db.get_stats()
        settings = db.get_settings()
        
        status_text = (
            "*Статус бота:*\n\n"
            f"Сохранено сообщений: *{stats['total_messages']}*\n"
            f"Сохранено медиафайлов: *{stats['total_media']}*\n\n"
            "*Настройки:*"
        )

        builder = InlineKeyboardBuilder()
        builder.button(
            text=f"Изменённые: {'🟢 ON' if settings['notify_edited'] else '🔴 OFF'}", 
            callback_data="toggle_edited"
        )
        builder.button(
            text=f"Удалённые: {'🟢 ON' if settings['notify_deleted'] else '🔴 OFF'}", 
            callback_data="toggle_deleted"
        )
        builder.adjust(1)
        
        await message.answer(
            status_text, 
            parse_mode="MarkdownV2",
            reply_markup=builder.as_markup()
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
    
    settings = db.get_settings()
    stats = db.get_stats()
    
    status_text = (
        "*Статус бота:*\n\n"
        f"Сохранено сообщений: *{stats['total_messages']}*\n"
        f"Сохранено медиафайлов: *{stats['total_media']}*\n\n"
        "*Настройки:*"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"Изменённые: {'🟢 ON' if settings['notify_edited'] else '🔴 OFF'}", 
        callback_data="toggle_edited"
    )
    builder.button(
        text=f"Удалённые: {'🟢 ON' if settings['notify_deleted'] else '🔴 OFF'}", 
        callback_data="toggle_deleted"
    )
    builder.adjust(1)
    
    await callback.message.edit_text(
        status_text,
        parse_mode="MarkdownV2",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.business_message()
async def message(message: types.Message):
    if str(message.from_user.id) != os.getenv("USER_ID"):
        await save_message(message)

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

    text = (
        f"✏️ @{escape_markdown(old_message['username'])} \\(ID: {old_message['user_id']}\\) edited message\n\n"
        f"from:\n{format_as_quote(escape_markdown(old_message['text']))}\n\n"
        f"to:\n{format_as_quote(escape_markdown(message.text or message.caption or ''))}"
    )
    await bot.send_message(
        chat_id=os.getenv("USER_ID"),
        text=text,
        parse_mode="MarkdownV2"
    )
    
    await save_message(message)

@dp.deleted_business_messages()
async def deleted_message(business_messages: types.BusinessMessagesDeleted):
    settings = db.get_settings()
    if not settings["notify_deleted"]:
        return

    for message_id in business_messages.message_ids:
        old_message = db.get_message(business_messages.chat.id, message_id)
        
        if not old_message or str(old_message['user_id']) == os.getenv("USER_ID"):
            continue

        text = f"🗑️ @{escape_markdown(old_message['username'])} \\(ID: {old_message['user_id']}\\) deleted message:\n\n{format_as_quote(escape_markdown(old_message['text']))}"
        
        if old_message['media_files']:
            sent_text = False
            sticker_message = None
            
            for media_type, media_path, file_id in old_message['media_files']:
                if media_type != "sticker" and not os.path.exists(media_path):
                    continue
                    
                if media_type == "photo":
                    await bot.send_photo(
                        chat_id=os.getenv("USER_ID"),
                        photo=types.FSInputFile(media_path),
                        caption=text if not sent_text else None,
                        parse_mode="MarkdownV2",
                        show_caption_above_media=True
                    )
                elif media_type == "video":
                    await bot.send_video(
                        chat_id=os.getenv("USER_ID"),
                        video=types.FSInputFile(media_path),
                        caption=text if not sent_text else None,
                        parse_mode="MarkdownV2",
                        show_caption_above_media=True
                    )
                elif media_type == "video_note":
                    video_note_message = await bot.send_video_note(
                        chat_id=os.getenv("USER_ID"),
                        video_note=types.FSInputFile(media_path)
                    )
                    await bot.send_message(
                        chat_id=os.getenv("USER_ID"),
                        text=text,
                        parse_mode="MarkdownV2",
                        reply_to_message_id=video_note_message.message_id
                    )
                    sent_text = True
                elif media_type == "voice":
                    await bot.send_voice(
                        chat_id=os.getenv("USER_ID"),
                        voice=types.FSInputFile(media_path),
                        caption=text if not sent_text else None,
                        parse_mode="MarkdownV2",
                        show_caption_above_media=True
                    )
                elif media_type == "audio":
                    await bot.send_audio(
                        chat_id=os.getenv("USER_ID"),
                        audio=types.FSInputFile(media_path),
                        caption=text if not sent_text else None,
                        parse_mode="MarkdownV2",
                        show_caption_above_media=True
                    )
                elif media_type == "animation":
                    await bot.send_animation(
                        chat_id=os.getenv("USER_ID"),
                        animation=file_id,
                        caption=text if not sent_text else None,
                        parse_mode="MarkdownV2",
                        show_caption_above_media=True
                    )
                elif media_type == "document":
                    await bot.send_document(
                        chat_id=os.getenv("USER_ID"),
                        document=types.FSInputFile(media_path),
                        caption=text if not sent_text else None,
                        parse_mode="MarkdownV2"
                    )
                elif media_type == "sticker":
                    sticker_message = await bot.send_sticker(
                        chat_id=os.getenv("USER_ID"),
                        sticker=file_id
                    )
                    continue
                sent_text = True
                
            if sticker_message:
                await bot.send_message(
                    chat_id=os.getenv("USER_ID"),
                    text=text,
                    parse_mode="MarkdownV2",
                    reply_to_message_id=sticker_message.message_id
                )
        else:
            await bot.send_message(
                chat_id=os.getenv("USER_ID"),
                text=text,
                parse_mode="MarkdownV2"
            )
            
        db.delete_message(business_messages.chat.id, message_id)

@dp.business_connection()
async def on_business_connection(event: types.BusinessConnection):
    if str(event.user.id) != os.getenv("USER_ID"):
        return

    if event.is_enabled:
        await bot.send_message(
            chat_id=event.user.id,
            text="_Бот успешно подключен к Telegram Business!_",
            parse_mode="MarkdownV2"
        )
    else:
        await bot.send_message(
            chat_id=event.user.id,
            text="_Бот успешно отключен от Telegram Business._",
            parse_mode="MarkdownV2"
        )

async def cleanup_messages():
    while True:
        deleted_count = db.cleanup_old_messages(hours=MESSAGES_LIFETIME)
        print(f"{datetime.now()}: Удалено {deleted_count} старых сообщений")

        await asyncio.sleep(CLEANUP_INTERVAL)

async def on_startup():
    deleted_count = db.cleanup_old_messages(hours=MESSAGES_LIFETIME)
    print(f"Удалено {deleted_count} старых сообщений")

    asyncio.create_task(cleanup_messages())

if __name__ == "__main__":
    dp.startup.register(on_startup)
    
    dp.run_polling(
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