import os

import pytest

from spybot.cli import initialize
from spybot.config import ConfigurationError, Settings


def test_init_never_replaces_key(tmp_path):
    key = tmp_path / "key"
    initialize(key, tmp_path / "data")
    original = key.read_bytes()
    with pytest.raises(ConfigurationError):
        initialize(key, tmp_path / "data")
    assert key.read_bytes() == original
    assert key.stat().st_mode & 0o777 == 0o600


def test_init_refuses_new_key_for_existing_archive(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "archive.db").write_bytes(b"old encrypted database")
    with pytest.raises(ConfigurationError):
        initialize(tmp_path / "key", data)
    assert not (tmp_path / "key").exists()


def test_private_paths_required(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TOKEN=1:synthetic-token-never-real\nUSER_ID=1\n")
    env.chmod(0o644)
    with pytest.raises(ConfigurationError, match="600"):
        Settings.load(env)
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    with pytest.raises(ConfigurationError, match="700"):
        Settings("1:synthetic", 1, data, tmp_path / "key").prepare_data_dir()


def test_key_must_be_private(tmp_path):
    key = tmp_path / "key"
    key.write_text(os.urandom(32).hex())
    key.chmod(0o644)
    with pytest.raises(ConfigurationError, match="600"):
        Settings("1:synthetic", 1, tmp_path / "data", key).read_key()
