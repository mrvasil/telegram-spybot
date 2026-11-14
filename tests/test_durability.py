import asyncio
import subprocess
import sys

import pytest

from spybot.archive import Archive
from spybot.config import Settings
from spybot.runtime import poll_loop


async def test_committed_inbox_survives_abrupt_process_death(tmp_path):
    path = tmp_path / "archive.db"
    key = bytes(range(32))
    code = """
import asyncio,os,sys
from pathlib import Path
from spybot.archive import Archive
async def main():
 a=await Archive.open(Path(sys.argv[1]),bytes(range(32)),1)
 await a.ingest_batch([{'update_id':42,'business_message':{'unknown_future_field':{'spoiler':'preserved'}}}])
 os._exit(19)
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code, str(path)], capture_output=True
    )
    assert result.returncode == 19
    archive = await Archive.open(path, key, 1)
    try:
        assert await archive.get_offset() == 43
        assert (await archive.pending_updates())[0]["payload"]["business_message"][
            "unknown_future_field"
        ]["spoiler"] == "preserved"
    finally:
        await archive.close()


async def test_poll_does_not_ack_when_durable_write_fails(tmp_path):
    offsets = []

    class Store:
        async def get_offset(self):
            return 7

        async def ingest_batch(self, updates):
            raise OSError("synthetic disk full")

    class API:
        async def updates(self, offset, poll_timeout):
            offsets.append(offset)
            return [{"update_id": 7, "business_message": {"text": "x"}}]

    settings = Settings("1:synthetic", 1, tmp_path, tmp_path / "key")
    with pytest.raises(OSError):
        await poll_loop(Store(), API(), settings, {})
    assert offsets == [7]  # No request with offset 8 was sent.
