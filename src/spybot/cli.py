"""Offline administration and explicit daemon entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from spybot import __version__
from spybot.config import ConfigurationError, Settings
from spybot.crypto import decrypt_chunks
from spybot.locking import InstanceLock


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Личный зашифрованный архив Telegram Business")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", default=".env")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Запустить приём событий и фоновые задачи")
    init = sub.add_parser("init", help="Создать отдельный ключ; существующие данные не изменяются")
    init.add_argument(
        "--key-file",
        type=Path,
        default=Path(
            os.getenv("ARCHIVE_KEY_FILE", "~/.config/telegram-spybot/archive.key")
        ).expanduser(),
    )
    init.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    sub.add_parser("status", help="Прочитать статистику локального архива без Telegram")
    health = sub.add_parser("health", help="Проверить свежесть состояния работающего процесса")
    health.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    sub.add_parser("backup", help="Создать зашифрованный backup при остановленном боте")
    restore = sub.add_parser("restore", help="Восстановить backup только в новый каталог")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", type=Path)
    restore.add_argument("--max-bytes", type=int, default=100 * 1024**3)
    export = sub.add_parser("export", help="Сформировать зашифрованный ZIP-экспорт")
    export.add_argument("--chat-id", type=int)
    export.add_argument("--include-media", action="store_true")
    decrypt = sub.add_parser("decrypt", help="Явно расшифровать экспорт в указанный новый файл")
    decrypt.add_argument("source", type=Path)
    decrypt.add_argument("destination", type=Path)
    legacy = sub.add_parser(
        "import-legacy", help="Проверить или импортировать старую SQLite без её изменения"
    )
    legacy.add_argument("source", type=Path)
    legacy.add_argument("--media-root", type=Path)
    legacy.add_argument(
        "--source-timezone",
        help="Часовой пояс старых дат без UTC offset; обязателен при их импорте",
    )
    legacy.add_argument(
        "--apply", action="store_true", help="Без этого флага выполняется только dry run"
    )
    retry = sub.add_parser("retry", help="Повторить ошибочные загрузки и обработку событий")
    retry.add_argument(
        "--include-uncertain",
        action="store_true",
        help="Повторить неоднозначные отправки; возможны дубликаты",
    )
    return parser


def initialize(key_file: Path, data_dir: Path) -> None:
    key_file = key_file.expanduser().resolve()
    data_dir = data_dir.expanduser().resolve()
    if key_file == data_dir or data_dir in key_file.parents:
        raise ConfigurationError("Храните ключ вне каталога данных")
    if key_file.exists():
        raise ConfigurationError("Ключ уже существует. Он не будет перезаписан")
    if (data_dir / "archive.db").exists():
        raise ConfigurationError(
            "Архив уже существует: восстановите прежний ключ, не создавайте новый"
        )
    key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(secrets.token_hex(32) + "\n")
        output.flush()
        os.fsync(output.fileno())
    print(
        f"Ключ создан: {key_file}. Сохраните отдельную резервную копию: потеря ключа делает архив недоступным."
    )


def health_check(data_dir: Path) -> bool:
    try:
        state = json.loads((data_dir / "health.json").read_text())
        now = time.time()
        return (
            state.get("state") == "running"
            and 0 <= now - state["updated_at"] < 90
            and 0 <= now - state.get("last_poll_ok", 0) < 180
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def _execute(args: argparse.Namespace, settings: Settings) -> None:
    from spybot.archive import Archive
    from spybot.operations import Operations, restore_backup

    key = settings.read_key()
    if args.command == "decrypt":
        try:
            with args.destination.open("xb") as output:
                os.chmod(args.destination, 0o600)
                for chunk in decrypt_chunks(args.source, key):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            # Do not remove a pre-existing destination when open('xb') fails.
            if not isinstance(sys.exception(), FileExistsError):
                args.destination.unlink(missing_ok=True)
            raise
        print(f"Расшифрованный файл: {args.destination}")
        return
    if args.command == "restore":
        manifest = await asyncio.to_thread(
            restore_backup, args.source, args.destination, key, args.max_bytes
        )
        restored = await Archive.open(args.destination / "archive.db", key, settings.owner_id)
        try:
            stats = await restored.stats()
            for name in ("messages", "versions", "media"):
                if stats[name] != manifest["stats"][name]:
                    raise ValueError("Restore count mismatch")
        finally:
            await restored.close()
        print(json.dumps({"restored": str(args.destination), "stats": stats}, ensure_ascii=False))
        return
    if args.command in (None, "run"):
        from spybot.runtime import run

        await run(settings)
        return
    if not (settings.data_dir / "archive.db").exists() and args.command not in {"import-legacy"}:
        raise ConfigurationError("Архив ещё не создан")
    settings.prepare_data_dir()
    with InstanceLock(settings.data_dir / ".instance.lock"):
        archive = await Archive.open(settings.data_dir / "archive.db", key, settings.owner_id)
        try:
            operations = Operations(archive, settings, key, asyncio.Lock())
            if args.command == "status":
                print(json.dumps(await archive.stats(), ensure_ascii=False, indent=2))
            elif args.command == "backup":
                print(await operations.backup())
            elif args.command == "export":
                path, size = await operations.export(
                    chat_id=args.chat_id, include_media=args.include_media
                )
                print(json.dumps({"encrypted_export": str(path), "zip_bytes": size}))
            elif args.command == "import-legacy":
                from spybot.legacy import import_legacy

                report = await import_legacy(
                    archive,
                    settings,
                    key,
                    args.source,
                    args.media_root or args.source.parent,
                    args.source_timezone,
                    apply=args.apply,
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
            elif args.command == "retry":
                print(
                    json.dumps(await archive.retry_failed(include_uncertain=args.include_uncertain))
                )
        finally:
            await archive.close()


def main() -> None:
    os.umask(0o077)
    args = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "init":
            initialize(args.key_file, args.data_dir)
        elif args.command == "health":
            if not health_check(args.data_dir):
                raise SystemExit(1)
        else:
            asyncio.run(_execute(args, Settings.load(args.env_file)))
    except (ConfigurationError, FileExistsError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        # Do not leak network URLs/tokens or private payloads through tracebacks.
        print(
            f"Операция не завершена: {type(error).__name__}. Данные и ключ не перезаписывайте.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
