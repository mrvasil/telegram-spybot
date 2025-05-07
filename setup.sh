#!/bin/bash

echo "Telegram Spy Bot Installation"
echo "------------------------"

if ! command -v docker &> /dev/null; then
    echo "Docker is not installed"
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "Docker Compose id not installed"
    exit 1
fi

echo -n "Enter your bot token: "
read BOT_TOKEN

echo -n "Enter your Telegram ID: "
read USER_ID

echo -n "Enter messages lifetime (default 24 hours): "
read MESSAGES_LIFETIME
MESSAGES_LIFETIME=${MESSAGES_LIFETIME:-24}

if [ -z "$BOT_TOKEN" ] || [ -z "$USER_ID" ]; then
    echo "You must fill in the bot token and Telegram ID"
    exit 1
fi

cat > .env << EOF
TOKEN=$BOT_TOKEN
USER_ID=$USER_ID
MESSAGES_LIFETIME=$MESSAGES_LIFETIME
CLEANUP_INTERVAL=3600
EOF

docker-compose down
docker volume rm telegram-spybot-media telegram-spybot-db 2>/dev/null || true

echo "Starting bot..."
docker-compose up --build -d

if [ $? -eq 0 ]; then
    echo "------------------------"
    echo "Bot started successfully"
else
    echo "------------------------"
    echo "Error starting bot"
fi 