from aiogram import Bot, Dispatcher, types
from redis.asyncio import Redis
from dotenv import load_dotenv
import os

load_dotenv()

bot = Bot(token=os.getenv("TOKEN"))
dp = Dispatcher()

redis = Redis()

EX_TIME = 60 * 60 * 24 * 7 # ужимается после 7 дней

async def set_message(message: types.Message):
    await redis.set(
        f"{message.chat.id}:{message.message_id}",
        message.model_dump_json(),
        ex=EX_TIME,
    )

@dp.business_message()
async def message(message: types.Message):
    await set_message(message)

@dp.edited_business_message()
async def edited_message(message: types.Message):
    model_dump = await redis.get(f"{message.chat.id}:{message.message_id}")
    await set_message(message)

    if not model_dump:
        return

    original_message = types.Message.model_validate_json(model_dump)
    if not original_message.from_user:
        return

    text = (
        f"✏️ {original_message.from_user.username} (ID: {original_message.from_user.id}) edited message\n\n"
        f"from:\n{original_message.text}\n\n"
        f"to:\n{message.text}"
    )
    await bot.send_message(
        chat_id=os.getenv("USER_ID"),
        text=text
    )

@dp.deleted_business_messages()
async def deleted_message(business_messages: types.BusinessMessagesDeleted):
    pipe = redis.pipeline()
    for message_id in business_messages.message_ids:
        pipe.get(f"{business_messages.chat.id}:{message_id}")
    messages_data = await pipe.execute()

    keys_to_delete = []
    for message_id, model_dump in zip(business_messages.message_ids, messages_data):
        if not model_dump:
            continue

        original_message = types.Message.model_validate_json(model_dump)
        if not original_message.from_user:
            continue

        text = f"🗑️ {original_message.from_user.username} (ID: {original_message.from_user.id}) deleted message:\n\n{original_message.text}"
        await bot.send_message(
            chat_id=os.getenv("USER_ID"),
            text=text
        )
        keys_to_delete.append(f"{business_messages.chat.id}:{message_id}")

    if keys_to_delete:
        await redis.delete(*keys_to_delete)

if __name__ == "__main__":
    dp.run_polling(
        bot,
        allowed_updates=[
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
    )