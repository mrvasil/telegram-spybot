from aiogram import Bot, Dispatcher, types
from dotenv import load_dotenv
import os

from database import Database
from media import download_media

load_dotenv()

bot = Bot(token=os.getenv("TOKEN"))
dp = Dispatcher()

db = Database()

async def save_message(message: types.Message):
    media_path, file_id = await db.save_message(message)
    
    if file_id:
        await download_media(bot, file_id, media_path)

@dp.business_message()
async def message(message: types.Message):
    await save_message(message)

@dp.edited_business_message()
async def edited_message(message: types.Message):
    old_message = db.get_message(message.chat.id, message.message_id)
    await save_message(message)

    if not old_message:
        return

    text = (
        f"✏️ @{old_message['username']} (ID: {old_message['user_id']}) edited message\n\n"
        f"from:\n{old_message['text']}\n\n"
        f"to:\n{message.text or message.caption or ''}"
    )
    await bot.send_message(
        chat_id=os.getenv("USER_ID"),
        text=text
    )

@dp.deleted_business_messages()
async def deleted_message(business_messages: types.BusinessMessagesDeleted):
    for message_id in business_messages.message_ids:
        old_message = db.get_message(business_messages.chat.id, message_id)
        
        if not old_message:
            continue

        text = f"🗑️ @{old_message['username']} (ID: {old_message['user_id']}) deleted message:\n\n{old_message['text']}"

        if old_message['media_path'] and os.path.exists(old_message['media_path']):
            media_path = old_message['media_path']
            
            if '_photo' in media_path:
                await bot.send_photo(
                    chat_id=os.getenv("USER_ID"),
                    photo=types.FSInputFile(media_path),
                    caption=text
                )
            elif '_video' in media_path:
                await bot.send_video(
                    chat_id=os.getenv("USER_ID"),
                    video=types.FSInputFile(media_path),
                    caption=text
                )
            elif '_voice' in media_path:
                await bot.send_voice(
                    chat_id=os.getenv("USER_ID"),
                    voice=types.FSInputFile(media_path),
                    caption=text
                )
            elif '_audio' in media_path:
                await bot.send_audio(
                    chat_id=os.getenv("USER_ID"),
                    audio=types.FSInputFile(media_path),
                    caption=text
                )
            elif '_document' in media_path:
                await bot.send_document(
                    chat_id=os.getenv("USER_ID"),
                    document=types.FSInputFile(media_path),
                    caption=text
                )
        else:
            await bot.send_message(
                chat_id=os.getenv("USER_ID"),
                text=text
            )
            
        db.delete_message(business_messages.chat.id, message_id)

if __name__ == "__main__":
    dp.run_polling(
        bot,
        allowed_updates=[
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
    )