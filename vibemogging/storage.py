"""Immutable, lossless media objects and atomic JSON metadata."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import zlib


class UserError(ValueError):
    """An actionable error safe to show in the editor."""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(kind: str, data: bytes) -> str:
    return hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()


def valid_id(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, root: Path):
        self.root = root
        (root / "objects").mkdir(parents=True, exist_ok=True)

    def path(self, object_id: str) -> Path:
        if not valid_id(object_id):
            raise UserError("Некорректный идентификатор объекта.")
        return self.root / "objects" / object_id[:2] / object_id[2:]

    def put(self, kind: str, data: bytes) -> str:
        object_id = digest(kind, data)
        path = self.path(object_id)
        if not path.exists():
            atomic_write(path, zlib.compress(data, level=3))
        return object_id

    def get(self, kind: str, object_id: str) -> bytes:
        try:
            data = zlib.decompress(self.path(object_id).read_bytes())
        except (OSError, zlib.error) as error:
            raise UserError(f"Не удалось прочитать объект {object_id[:12]}.") from error
        if digest(kind, data) != object_id:
            raise UserError(f"Повреждён объект {object_id[:12]}.")
        return data

    def put_json(self, kind: str, value) -> str:
        return self.put(kind, canonical(value))

    def get_json(self, kind: str, object_id: str):
        return json.loads(self.get(kind, object_id))
