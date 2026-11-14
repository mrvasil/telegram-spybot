"""Host-level singleton lock; a second consumer must never advance our offset."""

from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType
from typing import IO


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.file: IO[str] | None = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise RuntimeError("Другой процесс уже использует этот архив") from None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.file:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
