"""Finish a filesystem mutation before cancellation cleanup can touch its files."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def filesystem_call[T](function: Callable[..., T], *args) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass  # Original cancellation wins; caller removes temporary output.
        raise
