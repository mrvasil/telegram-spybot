"""Validated configuration. No network, keys or database side effects on import."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    pass


def _integer(name: str, default: int, minimum: int = 0) -> int:
    try:
        result = int(os.getenv(name, str(default)))
    except ValueError:
        raise ConfigurationError(f"{name}: требуется целое число") from None
    if result < minimum:
        raise ConfigurationError(f"{name}: значение должно быть не меньше {minimum}")
    return result


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    owner_id: int
    data_dir: Path
    key_file: Path
    api_url: str = "https://api.telegram.org"
    timezone: str = "Europe/Moscow"
    poll_timeout: int = 30
    max_file_bytes: int = 2_000_000_000
    min_free_bytes: int = 256 * 1024 * 1024
    retention_days: int = 0
    media_workers: int = 3
    backup_hours: int = 24
    backup_keep: int = 7

    @classmethod
    def load(cls, env_file: str | Path | None = ".env") -> Settings:
        if env_file is not None:
            env_path = Path(env_file)
            if env_path.exists() and os.name == "posix" and env_path.stat().st_mode & 0o077:
                raise ConfigurationError(
                    "Файл .env доступен другим пользователям: установите права 600"
                )
            load_dotenv(env_file, override=False)
        token = os.getenv("TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigurationError("TOKEN: укажите корректный токен бота")
        owner = _integer("USER_ID", 0, 1)
        data = Path(os.getenv("DATA_DIR", "data")).expanduser().resolve()
        key = (
            Path(os.getenv("ARCHIVE_KEY_FILE", "~/.config/telegram-spybot/archive.key"))
            .expanduser()
            .resolve()
        )
        if key == data or data in key.parents:
            raise ConfigurationError("ARCHIVE_KEY_FILE должен находиться вне DATA_DIR")
        api_url = os.getenv("BOT_API_URL", "https://api.telegram.org").rstrip("/")
        parsed = urlparse(api_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError(
                "BOT_API_URL: требуется URL сервера Bot API без учётных данных"
            )
        if parsed.scheme == "http" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "telegram-bot-api",
        }:
            raise ConfigurationError("Для внешнего BOT_API_URL требуется HTTPS")
        timezone = os.getenv("TIMEZONE", "Europe/Moscow")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            raise ConfigurationError("TIMEZONE: неизвестный часовой пояс") from None
        return cls(
            token,
            owner,
            data,
            key,
            api_url,
            timezone,
            min(_integer("POLL_TIMEOUT", 30, 1), 50),
            _integer("MAX_FILE_BYTES", 2_000_000_000, 1),
            _integer("MIN_FREE_BYTES", 256 * 1024 * 1024),
            _integer("RETENTION_DAYS", 0),
            min(_integer("MEDIA_WORKERS", 3, 1), 8),
            _integer("BACKUP_HOURS", 24),
            _integer("BACKUP_KEEP", 7, 1),
        )

    def read_key(self) -> bytes:
        try:
            mode = self.key_file.stat().st_mode
            if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise ConfigurationError(
                    "Файл ключа доступен другим пользователям: установите права 600"
                )
            key = bytes.fromhex(self.key_file.read_text().strip())
        except ConfigurationError:
            raise
        except (OSError, ValueError):
            raise ConfigurationError(
                "Не удалось прочитать ключ архива. Выполните spybot init; существующий ключ не заменяйте"
            ) from None
        if len(key) != 32:
            raise ConfigurationError("Ключ архива должен содержать 64 шестнадцатеричных символа")
        return key

    def prepare_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and self.data_dir.stat().st_mode & 0o077:
            raise ConfigurationError("DATA_DIR должен иметь права 700")
        for name in ("objects", "staging", "backups", "exports"):
            directory = self.data_dir / name
            directory.mkdir(exist_ok=True, mode=0o700)
            if os.name == "posix" and directory.stat().st_mode & 0o077:
                raise ConfigurationError("Подкаталоги DATA_DIR должны иметь права 700")
