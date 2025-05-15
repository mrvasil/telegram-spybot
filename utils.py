from aiogram import Bot, types
from aiogram.types import FSInputFile
import os
from datetime import datetime

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    chars_to_escape = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!',]
    for char in chars_to_escape:
        text = text.replace(char, "\\" + char)
    for char in chars_to_escape:
        text = text.replace(char, "\\" + char)
    text = text.replace("\n", "\n>")
    return text

def format_as_quote(text: str) -> str:
    if not text:
        return ""
    text = escape_markdown(text)
    return f"**>{text}||"

async def download_media(bot: Bot, file_id: str, path: str):
    if not os.path.exists(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, path)
    return path

async def save_message(bot: Bot, message: types.Message, db):
    if str(message.from_user.id) == os.getenv("USER_ID"):
        return
        
    saved_media = await db.save_message(message)
    
    for media_path, file_id in saved_media:
        await download_media(bot, file_id, media_path)

async def collect_media_from_message(bot: Bot, message: types.Message):
    media_files = []
    
    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        file_path = f"media/{message.chat.id}_{message.message_id}_photo.jpg"
        await download_media(bot, file_id, file_path)
        media_files.append(("photo", file_path, file_id))
    elif message.video:
        file_id = message.video.file_id
        file_path = f"media/{message.chat.id}_{message.message_id}_video.mp4"
        await download_media(bot, file_id, file_path)
        media_files.append(("video", file_path, file_id))
    elif message.video_note:
        file_id = message.video_note.file_id
        file_path = f"media/{message.chat.id}_{message.message_id}_video_note.mp4"
        await download_media(bot, file_id, file_path)
        media_files.append(("video_note", file_path, file_id))
    elif message.voice:
        file_id = message.voice.file_id
        file_path = f"media/{message.chat.id}_{message.message_id}_voice.ogg"
        await download_media(bot, file_id, file_path)
        media_files.append(("voice", file_path, file_id))
    elif message.audio:
        file_id = message.audio.file_id
        file_path = f"media/{message.chat.id}_{message.message_id}_audio.mp3"
        await download_media(bot, file_id, file_path)
        media_files.append(("audio", file_path, file_id))
    elif message.animation:
        file_id = message.animation.file_id
        file_path = f"media/{message.chat.id}_{message.message_id}_animation.gif"
        await download_media(bot, file_id, file_path)
        media_files.append(("animation", file_path, file_id))
    elif message.document:
        file_id = message.document.file_id
        file_path = f"media/{message.chat.id}_{message.message_id}_document.file"
        await download_media(bot, file_id, file_path)
        media_files.append(("document", file_path, file_id))
    elif message.sticker:
        file_id = message.sticker.file_id
        media_files.append(("sticker", "", file_id))
        
    return media_files

async def send_media_message(bot: Bot, media_files, text, reply_to_message_id=None):
    if not media_files:
        await bot.send_message(
            chat_id=os.getenv("USER_ID"),
            text=text,
            parse_mode="MarkdownV2",
            reply_to_message_id=reply_to_message_id
        )
        return True
    
    for media_type, media_path, file_id in media_files:
        if media_type != "sticker" and not os.path.exists(media_path):
            continue
            
        if media_type == "photo":
            await bot.send_photo(
                chat_id=os.getenv("USER_ID"),
                photo=types.FSInputFile(media_path),
                caption=text,
                parse_mode="MarkdownV2",
                show_caption_above_media=True,
                reply_to_message_id=reply_to_message_id
            )
            return True
        elif media_type == "video":
            await bot.send_video(
                chat_id=os.getenv("USER_ID"),
                video=types.FSInputFile(media_path),
                caption=text,
                parse_mode="MarkdownV2",
                show_caption_above_media=True,
                reply_to_message_id=reply_to_message_id
            )
            return True
        elif media_type == "video_note":
            video_note_message = await bot.send_video_note(
                chat_id=os.getenv("USER_ID"),
                video_note=types.FSInputFile(media_path),
                reply_to_message_id=reply_to_message_id
            )
            await bot.send_message(
                chat_id=os.getenv("USER_ID"),
                text=text,
                parse_mode="MarkdownV2",
                reply_to_message_id=video_note_message.message_id
            )
            return True
        elif media_type == "voice":
            await bot.send_voice(
                chat_id=os.getenv("USER_ID"),
                voice=types.FSInputFile(media_path),
                caption=text,
                parse_mode="MarkdownV2",
                reply_to_message_id=reply_to_message_id
            )
            return True
        elif media_type == "audio":
            await bot.send_audio(
                chat_id=os.getenv("USER_ID"),
                audio=types.FSInputFile(media_path),
                caption=text,
                parse_mode="MarkdownV2",
                reply_to_message_id=reply_to_message_id
            )
            return True
        elif media_type == "animation":
            await bot.send_animation(
                chat_id=os.getenv("USER_ID"),
                animation=file_id,
                caption=text,
                parse_mode="MarkdownV2",
                show_caption_above_media=True,
                reply_to_message_id=reply_to_message_id
            )
            return True
        elif media_type == "document":
            await bot.send_document(
                chat_id=os.getenv("USER_ID"),
                document=types.FSInputFile(media_path),
                caption=text,
                parse_mode="MarkdownV2",
                reply_to_message_id=reply_to_message_id
            )
            return True
        elif media_type == "sticker":
            sticker_message = await bot.send_sticker(
                chat_id=os.getenv("USER_ID"),
                sticker=file_id,
                reply_to_message_id=reply_to_message_id
            )
            await bot.send_message(
                chat_id=os.getenv("USER_ID"),
                text=text,
                parse_mode="MarkdownV2",
                reply_to_message_id=sticker_message.message_id
            )
            return True
            
    await bot.send_message(
        chat_id=os.getenv("USER_ID"),
        text=text,
        parse_mode="MarkdownV2",
        reply_to_message_id=reply_to_message_id
    )
    return True 