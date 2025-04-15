# Telegram Spy Bot

Телеграм бот для отслеживания удаленных и измененных сообщений в лс.

## Быстрая установка

```bash
git clone https://github.com/talyamm/telegram-spybot.git && cd telegram-spybot && chmod +x setup.sh && ./setup.sh
```
 
## Ручная установка

### 1. Клонировать репозиторий
```bash
git clone https://github.com/talyamm/telegram-spybot.git
cd telegram-spybot
```

### 2. Создать файл `.env`:
```
TOKEN=
USER_ID=
MESSAGES_LIFETIME=24
CLEANUP_INTERVAL=3600
```

### 3. Запустить через Docker Compose
```bash
docker-compose up -d
```