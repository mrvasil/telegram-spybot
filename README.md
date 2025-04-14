# Telegram Spy Bot

Телеграм бот для отслеживания удаленных и измененных сообщений в лс.

## Установка и запуск

1. Клонировать репозиторий
```bash
git clone https://github.com/talyamm/telegram-spybot.git
cd telegram-spybot
```

2. Установить зависимости
```bash
pip install -r requirements.txt
```

3. Создать файл `.env`:
```
TOKEN=токен_бота
USER_ID=ваш_телеграм_id
MESSAGES_LIFETIME=24
CLEANUP_INTERVAL=3600
```

### Запуск бота

```bash
python main.py
```

Всё медиа сохраняется в `media/`.