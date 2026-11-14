import os

import pytest

from spybot.crypto import CHUNK_SIZE, EncryptedWriter, IntegrityError, decrypt_chunks, verify_object


def test_encrypted_stream_roundtrip(tmp_path):
    key = os.urandom(32)
    path = tmp_path / "object"
    content = b"private-content-" + os.urandom(CHUNK_SIZE * 2 + 5)
    writer = EncryptedWriter(path, key)
    for i in range(0, len(content), 127):
        writer.write(content[i : i + 127])
    digest, size = writer.finish()
    assert b"private-content-" not in path.read_bytes()
    assert b"".join(decrypt_chunks(path, key)) == content
    assert verify_object(path, key) == (digest, size)
    with pytest.raises(IntegrityError):
        list(decrypt_chunks(path, os.urandom(32)))


@pytest.mark.parametrize("mutation", ["truncate", "flip", "append", "remove_eof"])
def test_authentication_rejects_corruption(tmp_path, mutation):
    key = os.urandom(32)
    path = tmp_path / "object"
    writer = EncryptedWriter(path, key)
    writer.write(b"hello" * 100)
    writer.finish()
    data = bytearray(path.read_bytes())
    if mutation == "truncate":
        data = data[:80]
    if mutation == "flip":
        data[60] ^= 1
    if mutation == "append":
        data += b"x"
    if mutation == "remove_eof":
        data = data[:-20]
    path.write_bytes(data)
    with pytest.raises(IntegrityError):
        list(decrypt_chunks(path, key))


def test_empty_and_abort(tmp_path):
    key = os.urandom(32)
    path = tmp_path / "object"
    writer = EncryptedWriter(path, key)
    writer.finish()
    assert list(decrypt_chunks(path, key)) == []
    other = tmp_path / "other"
    writer = EncryptedWriter(other, key)
    writer.abort()
    assert not other.exists()
