# Telegram Spy Bot

Телеграм бот для отслеживания удаленных и измененных сообщений в лс.

## Установка и запуск

1. Клонировать репозиторий
```bash
git clone https://github.com/talyamm/telegram-spybot.git
cd telegram-spybot
```
2. Создать файл `.env`:
```
TOKEN=
USER_ID=
MESSAGES_LIFETIME=24
CLEANUP_INTERVAL=3600
```

### Вариант 1: Запустить через Docker Compose
```bash
docker-compose up -d
```
### Вариант 2: Стандартный запуск

1. Установить зависимости
```bash
pip install -r requirements.txt
```

2. Запустить бота
```bash
python main.py
```