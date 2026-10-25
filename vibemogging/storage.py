"""Immutable, lossless media objects and atomic JSON metadata."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import OrderedDict
from pathlib import Path
import sqlite3
import tempfile
import threading
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
    def __init__(self, root: Path, *, cache_bytes=32 * 1024 * 1024):
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = max(0, cache_bytes)
        self._decoders = OrderedDict()
        self._db = None

    def _connection(self, *, create=False):
        if self._db is None and (create or (self.root / "sources.sqlite3").exists()):
            self._db = sqlite3.connect(self.root / "sources.sqlite3", check_same_thread=False, timeout=10)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS recipes (id TEXT PRIMARY KEY, recipe TEXT NOT NULL)")
            self._db.commit()
        return self._db

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
        with self._lock:
            return self._get(kind, object_id, set())

    def _get(self, kind, object_id, visiting):
        if kind == "video" and object_id in self._cache:
            self._cache.move_to_end(object_id)
            return self._cache[object_id]
        if object_id in visiting or len(visiting) > 64:
            raise UserError("Циклическая или слишком глубокая зависимость кадров.")
        visiting = visiting | {object_id}
        path = self.path(object_id)
        if kind == "video" and not path.exists():
            recipe = self.recipe(object_id)
            if not recipe:
                raise UserError(f"Не найден кадр {object_id[:12]}.")
            if recipe["type"] == "source":
                from .video_sources import FrameDecoder
                profile = {key: recipe[key] for key in ("width", "height", "fps", "offset", "normalization")}
                key = canonical({"asset": recipe["asset"], **profile})
                decoder = self._decoders.get(key)
                if not decoder:
                    decoder = FrameDecoder(self.asset_path(recipe["asset"]), profile)
                    self._decoders[key] = decoder
                self._decoders.move_to_end(key)
                while len(self._decoders) > 2:
                    _, old = self._decoders.popitem(last=False)
                    old.close()
                try:
                    data = decoder.read(recipe["frame"])
                except UserError:
                    decoder.start(0, accurate=True)
                    data = decoder.read(recipe["frame"], accurate=True)
                if digest(kind, data) != object_id:
                    # Fast seeking and timestamps can disagree for VFR, delayed
                    # streams or codec rounding. Reconstruct from the full profile.
                    decoder.start(0, accurate=True)
                    data = decoder.read(recipe["frame"], accurate=True)
            else:
                table = self._get("lut", recipe["table"], visiting)
                if len(table) != 256:
                    raise UserError("Повреждена таблица яркости кадра.")
                data = self._get("video", recipe["base"], visiting).translate(table)
        else:
            try:
                data = zlib.decompress(path.read_bytes())
            except (OSError, zlib.error) as error:
                raise UserError(f"Не удалось прочитать объект {object_id[:12]}.") from error
        if digest(kind, data) != object_id:
            raise UserError(f"Объект {object_id[:12]} не воспроизводится точно. Проверьте исходник и версию FFmpeg.")
        if kind == "video" and len(data) <= self._cache_limit:
            if object_id not in self._cache:
                self._cache_bytes += len(data)
            self._cache[object_id] = data
            while self._cache_bytes > self._cache_limit:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= len(old)
        return data

    def asset_path(self, asset_id):
        if not valid_id(asset_id):
            raise UserError("Некорректный идентификатор исходника.")
        return self.root / "assets" / asset_id[:2] / asset_id[2:]

    def add_asset(self, path: Path):
        path = Path(path)
        resolved, asset_root = path.resolve(), (self.root / "assets").resolve()
        relative = resolved.relative_to(asset_root) if resolved.is_relative_to(asset_root) else None
        if relative is not None and len(relative.parts) == 2:
            asset_id = "".join(relative.parts)
            if valid_id(asset_id) and self.asset_path(asset_id).is_file():
                return asset_id
        # Streaming import also handles sources too large to fit in memory.
        with path.open("rb") as source:
            return self.import_asset(source, None, path.stat().st_size)

    def import_asset(self, stream, expected_id, size):
        if expected_id is not None and not valid_id(expected_id):
            raise UserError("Некорректная контрольная сумма исходника.")
        if not isinstance(size, int) or size <= 0:
            raise UserError("Пустой или некорректный исходный файл.")
        directory = self.root / "assets"
        directory.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".import-", dir=directory)
        hasher = hashlib.sha256(b"asset\0")
        try:
            with os.fdopen(fd, "wb") as output:
                remaining = size
                while remaining:
                    chunk = stream.read(min(8 * 1024 * 1024, remaining))
                    if not chunk:
                        raise UserError("Исходный файл передан не полностью.")
                    hasher.update(chunk)
                    output.write(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise UserError("Размер исходного файла не совпадает с описанием.")
                output.flush()
                os.fsync(output.fileno())
            asset_id = hasher.hexdigest()
            if expected_id is not None and asset_id != expected_id:
                raise UserError("Контрольная сумма исходного файла не совпадает.")
            target = self.asset_path(asset_id)
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                os.replace(temporary, target)
            return asset_id
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def recipe(self, video_id):
        if not valid_id(video_id):
            raise UserError("Некорректный идентификатор кадра.")
        if self.path(video_id).exists():
            return None
        with self._lock:
            db = self._connection()
            row = db.execute("SELECT recipe FROM recipes WHERE id=?", (video_id,)).fetchone() if db else None
        return json.loads(row[0]) if row else None

    def import_recipe(self, video_id, recipe):
        if not valid_id(video_id) or not isinstance(recipe, dict):
            raise UserError("Некорректное описание кадра.")
        if recipe.get("type") == "source":
            required = {"type", "asset", "width", "height", "fps", "offset", "frame", "normalization"}
            try:
                if set(recipe) != required:
                    raise ValueError
                w, h = recipe["width"], recipe["height"]
                n, d = recipe["fps"]
                if (not all(type(v) is int for v in (w, h, n, d, recipe["frame"])) or
                    w < 2 or h < 2 or w * h > 3840 * 2160 or w % 2 or h % 2 or
                    n <= 0 or d <= 0 or n / d > 240 or recipe["frame"] < 0 or
                    recipe["normalization"] not in (1, 2) or
                    not math.isfinite(float(recipe["offset"])) or float(recipe["offset"]) < 0 or
                    not self.asset_path(recipe["asset"]).is_file()):
                    raise ValueError
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                raise UserError("Некорректная ссылка на исходный видеокадр.") from error
        elif recipe.get("type") == "lut":
            if (set(recipe) != {"type", "base", "table"} or not valid_id(recipe.get("base")) or
                not valid_id(recipe.get("table")) or recipe["base"] == video_id):
                raise UserError("Некорректная зависимость эффекта.")
        else:
            raise UserError("Неизвестный способ хранения кадра.")
        with self._lock:
            if not self.path(video_id).exists():
                self._connection(create=True).execute("INSERT OR IGNORE INTO recipes(id, recipe) VALUES (?, ?)",
                                                      (video_id, canonical(recipe).decode()))

    def register_source_frame(self, asset_id, profile, frame_index, rgb):
        video_id = digest("video", rgb)
        self.import_recipe(video_id, {"type": "source", "asset": asset_id, **profile, "frame": frame_index})
        return video_id

    def put_video_lut(self, base_id, table, rendered=None):
        if not isinstance(table, bytes) or len(table) != 256:
            raise UserError("Таблица яркости должна содержать 256 значений.")
        if rendered is None:
            rendered = self.get("video", base_id).translate(table)
        video_id = digest("video", rendered)
        if video_id == base_id:
            return base_id
        previous = self.recipe(base_id)
        if previous and previous["type"] == "lut":
            table = self.get("lut", previous["table"]).translate(table)
            base_id = previous["base"]
        table_id = self.put("lut", table)
        self.import_recipe(video_id, {"type": "lut", "base": base_id, "table": table_id})
        return video_id

    def has(self, kind, object_id):
        return self.path(object_id).is_file() or (kind == "video" and self.recipe(object_id) is not None)

    def export_plan(self, references):
        objects, recipes, assets, visited = [], {}, set(), set()
        pending = list(references)
        while pending:
            kind, object_id = pending.pop()
            if (kind, object_id) in visited:
                continue
            visited.add((kind, object_id))
            if not self.has(kind, object_id):
                raise UserError(f"В истории не хватает объекта {object_id[:12]}.")
            recipe = self.recipe(object_id) if kind == "video" else None
            if recipe:
                recipes[object_id] = recipe
                if recipe["type"] == "source":
                    assets.add(recipe["asset"])
                else:
                    pending.extend((("video", recipe["base"]), ("lut", recipe["table"])))
            else:
                objects.append({"kind": kind, "id": object_id})
        return {"objects": sorted(objects, key=lambda r: (r["kind"], r["id"])),
                "recipes": recipes, "assets": sorted(assets)}

    def flush(self):
        with self._lock:
            if self._db:
                self._db.commit()

    def stats(self):
        def files(path):
            return [p for p in path.rglob("*") if p.is_file()] if path.exists() else []
        objects, assets = files(self.root / "objects"), files(self.root / "assets")
        with self._lock:
            db = self._connection()
            count = db.execute("SELECT count(*) FROM recipes").fetchone()[0] if db else 0
        return {"object_bytes": sum(p.stat().st_size for p in objects),
                "asset_bytes": sum(p.stat().st_size for p in assets),
                "index_bytes": sum(p.stat().st_size for p in self.root.glob("sources.sqlite3*")),
                "total_bytes": sum(p.stat().st_size for p in files(self.root)),
                "frame_cache_bytes": self._cache_bytes,
                "objects": len(objects), "assets": len(assets), "recipes": count}

    def close(self):
        with self._lock:
            for decoder in self._decoders.values():
                decoder.close()
            self._decoders.clear()
            self._cache.clear()
            self._cache_bytes = 0
            if self._db is not None:
                self._db.close()
                self._db = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def put_json(self, kind: str, value) -> str:
        self.flush()
        return self.put(kind, canonical(value))

    def get_json(self, kind: str, object_id: str):
        return json.loads(self.get(kind, object_id))
