# Telegram Spy Bot

> [!WARNING]
> Для работы бота требуется Telegram Premium

Телеграм бот для отслеживания удаленных и измененных сообщений в лс.

### Установка Docker

```bash
curl -sSL https://get.docker.com | sh
exit
```

## Быстрая установка

```bash
git clone https://github.com/talyamm/telegram-spybot.git && cd telegram-spybot && sudo chmod +x setup.sh && ./setup.sh
```
 
## Ручная установка

### 1. Клонировать репозиторий
```bash
git clone https://github.com/talyamm/telegram-spybot.git
cd telegram-spybot
```

### 2. Создать файл `.env`:
```bash
cp .env.example .env
nano .env
```

### 3. Запустить через Docker Compose
```bash
docker-compose up -d
```