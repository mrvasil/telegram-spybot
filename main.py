from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import os
import asyncio
from datetime import datetime

from database import Database

load_dotenv()

bot = Bot(token=os.getenv("TOKEN"))
dp = Dispatcher()

db = Database()

MESSAGES_LIFETIME = int(os.getenv("MESSAGES_LIFETIME", 24))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", 3600))

def escape_markdown(text: str) -> str:
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def format_as_quote(text: str) -> str:
    if not text:
        return ""
    text = escape_markdown(text)
    lines = text.split('\n')
    return '\n'.join(f'> {line}' if line.strip() else '>' for line in lines)

async def download_media(bot: Bot, file_id: str, path: str):
    if not os.path.exists(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, path)
    return path

async def save_message(message: types.Message):
    if str(message.from_user.id) == os.getenv("USER_ID"):
        return
        
    saved_media = await db.save_message(message)
    
    for media_path, file_id in saved_media:
        await download_media(bot, file_id, media_path)

def get_status_message():
    settings = db.get_settings()
    stats = db.get_stats()
    
    status_text = (
        "*Bot Status:*\n\n"
        f"Messages saved: *{stats['total_messages']}*\n"
        f"Media files saved: *{stats['total_media']}*\n\n"
        "*Settings:*"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"Edited: {'🟢 ON' if settings['notify_edited'] else '🔴 OFF'}", 
        callback_data="toggle_edited"
    )
    builder.button(
        text=f"Deleted: {'🟢 ON' if settings['notify_deleted'] else '🔴 OFF'}", 
        callback_data="toggle_deleted"
    )
    builder.adjust(1)
    
    return status_text, builder.as_markup()

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
        status_text, reply_markup = get_status_message()
        
        await message.answer(
            status_text, 
            parse_mode="MarkdownV2",
            reply_markup=reply_markup
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
    elif action.startswith("history_"):
        _, chat_id, message_id = action.split("_")
        history = list(reversed(db.get_message_history(int(chat_id), int(message_id))))
        text = "📝 Edit History:\n\n"
        for i, (msg_text, edited_at) in enumerate(history):
            dt = datetime.fromisoformat(edited_at)
            formatted_time = dt.strftime("%H:%M:%S")
            text += f"_{formatted_time}_\n{format_as_quote(msg_text)}\n"
            if i < len(history) - 1:
                text += "↓\n"
        current_message = db.get_message(int(chat_id), int(message_id))
        if current_message:
            current_time = datetime.now().strftime("%H:%M:%S")
            if history:
                text += "↓\n"
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

    db.save_message_history(message.chat.id, message.message_id, old_message['text'])

    builder = InlineKeyboardBuilder()
    builder.button(
        text="Edit History",
        callback_data=f"history_{message.chat.id}_{message.message_id}"
    )

    text = (
        f"✏️ @{old_message['username']} изменил сообщение\n"
        f"\n{format_as_quote(old_message['text'])}\n↓"
        f"\n{format_as_quote(message.md_text or message.caption or '')}"
    )
    
    await bot.send_message(
        chat_id=os.getenv("USER_ID"),
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=builder.as_markup()
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

        text = f"🗑️ @{escape_markdown(old_message['username'])} deleted message:\n\n"
        
        if old_message['is_forwarded'] and old_message['forward_from']:
            text += f"_Forwarded from {escape_markdown(old_message['forward_from'])}_\n"
            
        text += f"{format_as_quote(old_message['text'])}"
        
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
        text_="_Бот успешно отключен от Telegram Business._"
    else:
        text_="_Бот успешно подключен к Telegram Business._"
    
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
    asyncio.run(main())