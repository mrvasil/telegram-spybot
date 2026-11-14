"""Bounded-memory authenticated file storage using standard AES-256-GCM.

Each file has a fresh HKDF salt. Each chunk (including an authenticated EOF)
uses its sequence number as nonce and authenticates the complete header.
No plaintext temporary file is required for downloads or uploads.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"SPYARC\x01\n"
CHUNK_SIZE = 256 * 1024
HEADER_SIZE = len(MAGIC) + 32


class IntegrityError(ValueError):
    """Encrypted object is incomplete, corrupt or uses a different key."""


def _cipher(master: bytes, salt: bytes) -> AESGCM:
    if len(master) != 32:
        raise ValueError("Archive key must be 32 bytes")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"spybot-file-v1").derive(
        master
    )
    return AESGCM(key)


class EncryptedWriter:
    def __init__(self, path: Path, key: bytes):
        self.path = path
        self.header = MAGIC + os.urandom(32)
        self.cipher = _cipher(key, self.header[len(MAGIC) :])
        self.stream: BinaryIO = path.open("xb")
        os.chmod(path, 0o600)
        self.stream.write(self.header)
        self.pending = bytearray()
        self.index = 0
        self.size = 0
        self.digest = hashlib.sha256()
        self.closed = False

    def _record(self, content: bytes) -> None:
        length = struct.pack(">I", len(content))
        sequence = self.index.to_bytes(12, "big")
        self.stream.write(length)
        self.stream.write(self.cipher.encrypt(sequence, content, self.header + sequence + length))
        self.index += 1

    def write(self, chunk: bytes) -> None:
        if self.closed:
            raise ValueError("Writer closed")
        self.digest.update(chunk)
        self.size += len(chunk)
        self.pending.extend(chunk)
        while len(self.pending) >= CHUNK_SIZE:
            self._record(bytes(self.pending[:CHUNK_SIZE]))
            del self.pending[:CHUNK_SIZE]

    def finish(self) -> tuple[str, int]:
        if self.closed:
            raise ValueError("Writer closed")
        try:
            if self.pending:
                self._record(bytes(self.pending))
                self.pending.clear()
            self._record(b"")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            return self.digest.hexdigest(), self.size
        finally:
            self.stream.close()
            self.closed = True

    def abort(self) -> None:
        if not self.closed:
            self.stream.close()
            self.closed = True
        self.path.unlink(missing_ok=True)


def decrypt_chunks(path: Path, key: bytes) -> Iterator[bytes]:
    with path.open("rb") as stream:
        header = stream.read(HEADER_SIZE)
        if len(header) != HEADER_SIZE or not header.startswith(MAGIC):
            raise IntegrityError("Invalid encrypted archive header")
        cipher = _cipher(key, header[len(MAGIC) :])
        index = 0
        while True:
            length = stream.read(4)
            if len(length) != 4:
                raise IntegrityError("Missing authenticated end of archive")
            size = struct.unpack(">I", length)[0]
            if size > CHUNK_SIZE:
                raise IntegrityError("Invalid encrypted chunk size")
            encrypted = stream.read(size + 16)
            if len(encrypted) != size + 16:
                raise IntegrityError("Truncated encrypted chunk")
            sequence = index.to_bytes(12, "big")
            try:
                content = cipher.decrypt(sequence, encrypted, header + sequence + length)
            except InvalidTag:
                raise IntegrityError("Archive authentication failed") from None
            if not size:
                if stream.read(1):
                    raise IntegrityError("Unexpected bytes after archive end")
                return
            yield content
            index += 1


def verify_object(path: Path, key: bytes) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in decrypt_chunks(path, key):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def encrypt_file(source: Path, destination: Path, key: bytes) -> tuple[str, int]:
    writer = EncryptedWriter(destination, key)
    try:
        with source.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                writer.write(chunk)
        return writer.finish()
    except BaseException:
        writer.abort()
        raise


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
