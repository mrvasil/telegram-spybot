# Telegram Spy Bot

Телеграм бот для отслеживания удленных и изменения сообщений в лс.

## Установка и запуск

1. Клонировать репозиторий
```bash
git clone https://github.com/username/telegram-spybot.git
cd telegram-spybot
```

2. Установить зависимости
```bash
pip install -r requirements.txt
```

3. Создать файл `.env`:
```
TOKEN=окен_бота
USER_ID=телеграм_id
```

### Запуск бота

```bash
python main.py
```

Всё медиа сохраняется в `media/`.