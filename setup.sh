#!/bin/bash

echo "Установка Telegram Spy Bot"
echo "------------------------"

if ! command -v docker &> /dev/null; then
    echo "Docker не установлен. Пожалуйста, установите Docker перед запуском скрипта."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "Docker Compose не установлен. Пожалуйста, установите Docker Compose перед запуском скрипта."
    exit 1
fi

echo -n "Введите токен бота: "
read BOT_TOKEN

echo -n "Введите ваш Telegram ID: "
read USER_ID

if [ -z "$BOT_TOKEN" ] || [ -z "$USER_ID" ]; then
    echo "Ошибка: Токен бота и Telegram ID обязательны для заполнения"
    exit 1
fi

cat > .env << EOF
TOKEN=$BOT_TOKEN
USER_ID=$USER_ID
MESSAGES_LIFETIME=24
CLEANUP_INTERVAL=3600
EOF

docker-compose down
docker volume rm telegram-spybot-media telegram-spybot-db 2>/dev/null || true

echo "Запуск бота..."
docker-compose up --build -d

if [ $? -eq 0 ]; then
    echo "------------------------"
    echo "Бот успешно запущен!"
else
    echo "------------------------"
    echo "Произошла ошибка при запуске бота"
fi 