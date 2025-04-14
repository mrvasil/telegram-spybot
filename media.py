import os
from aiogram import Bot

async def download_media(bot: Bot, file_id: str, path: str):
    if not os.path.exists(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, path)
    return path 