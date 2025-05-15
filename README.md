# 🕵️‍♂️ Telegram Spy Bot

<div align="center">

![Telegram](https://img.shields.io/badge/Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)
![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)

**Бот для отслеживания удаленных и измененных сообщений в Telegram**

</div>

## ⚠️ Требования

> **Внимание!** Для работы бота требуется **Telegram Premium**

## 📋 Содержание

- [Описание](#-описание)
- [Установка](#-установка)
  - [Быстрая установка](#быстрая-установка)
  - [Ручная установка](#ручная-установка)
- [Docker](#-docker)

## 📝 Описание

Telegram Spy Bot - это инструмент для отслеживания удаленных и измененных сообщений в личных чатах Telegram. Бот позволяет видеть содержимое сообщений, даже если они были удалены или отредактированы.

## 🚀 Установка

### Быстрая установка

```bash
git clone https://github.com/talyamm/telegram-spybot.git && cd telegram-spybot && sudo chmod +x setup.sh && ./setup.sh
```

### Ручная установка

1. **Клонировать репозиторий**
   ```bash
   git clone https://github.com/talyamm/telegram-spybot.git
   cd telegram-spybot
   ```

2. **Создать файл `.env`**
   ```bash
   cp .env.example .env
   nano .env
   ```

3. **Запустить через Docker Compose**
   ```bash
   docker-compose up -d
   ```

## 🐳 Docker

Для установки Docker выполните:

```bash
curl -sSL https://get.docker.com | sh
exit
```