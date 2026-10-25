"""Test double of the compact Store contract (docs/COMPACT_STORAGE_CONTRACT.md).

Only for isolated tests while the primary implementation is not merged. It keeps
assets and recipes on disk, rebuilds frames with FFmpeg and checks every content
ID, so exchange/migration logic is exercised against real decoding. Production
code always uses vibemogging.storage.Store.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from vibemogging import media
from vibemogging.storage import Store, UserError, atomic_write, canonical, digest, valid_id


def real_store_is_compact():
    return all(hasattr(Store, name) for name in ("add_asset", "register_source_frame", "export_plan"))


class FakeCompactStore(Store):
    def __init__(self, root: Path):
        super().__init__(Path(root))
        (self.root / "fake-assets").mkdir(exist_ok=True)
        self.recipes_file = self.root / "fake-recipes.json"
        self.recipes = json.loads(self.recipes_file.read_text()) if self.recipes_file.exists() else {}
        self.decoded = {}

    def flush(self):
        atomic_write(self.recipes_file, canonical(self.recipes))

    def close(self):
        self.flush()
        self.decoded.clear()

    def stats(self):
        objects = sum(p.stat().st_size for p in (self.root / "objects").rglob("*") if p.is_file())
        assets = sum(p.stat().st_size for p in (self.root / "fake-assets").glob("*"))
        return {"object_bytes": objects, "asset_bytes": assets, "index_bytes": 0, "frame_cache_bytes": 0,
                "total_bytes": objects + assets, "objects": 0, "assets": 0, "recipes": len(self.recipes)}

    # assets
    def asset_path(self, asset_id):
        if not valid_id(asset_id):
            raise UserError("bad asset")
        return self.root / "fake-assets" / asset_id

    def add_asset(self, path: Path):
        asset_id = digest("asset", Path(path).read_bytes())
        if not self.asset_path(asset_id).exists():
            shutil.copyfile(path, self.asset_path(asset_id))
        return asset_id

    def import_asset(self, stream, expected_id, size):
        data = stream.read(size + 1)
        if len(data) != size or digest("asset", data) != expected_id:
            raise UserError("Исходный файл повреждён.")
        atomic_write(self.asset_path(expected_id), data)
        return expected_id

    # recipes
    def register_source_frame(self, asset_id, profile, frame_index, rgb):
        video_id = digest("video", rgb)
        self.recipes.setdefault(video_id, {"type": "source", "asset": asset_id, "width": profile["width"],
                                           "height": profile["height"], "fps": list(profile["fps"]),
                                           "offset": profile.get("offset", 0.0),
                                           "normalization": profile.get("normalization", 2),
                                           "frame": frame_index})
        return video_id

    def recipe(self, video_id):
        return self.recipes.get(video_id)

    def import_recipe(self, video_id, recipe):
        if not valid_id(video_id) or recipe.get("type") not in {"source", "lut"}:
            raise UserError("Некорректный рецепт.")
        self.recipes.setdefault(video_id, recipe)

    def has(self, kind, object_id):
        return (kind == "video" and object_id in self.recipes) or self.path(object_id).is_file()

    def get(self, kind, object_id):
        if kind == "video" and object_id in self.recipes and not self.path(object_id).is_file():
            recipe = self.recipes[object_id]
            if recipe["type"] != "source":
                raise UserError("LUT recipes are not modelled by the test double")
            data = self._frames(recipe)[recipe["frame"]]
            if digest("video", data) != object_id:
                raise UserError(f"Кадр {object_id[:12]} не воспроизводится из исходника.")
            return data
        return super().get(kind, object_id)

    def _frames(self, recipe):
        key = recipe["asset"]
        if key not in self.decoded:
            size = recipe["width"] * recipe["height"] * 3
            raw = subprocess.run(media.command("-i", self.asset_path(key), "-f", "rawvideo", "-pix_fmt", "rgb24",
                                               "pipe:1"), capture_output=True, check=True).stdout
            self.decoded[key] = [raw[i:i + size] for i in range(0, len(raw), size)]
        return self.decoded[key]

    def export_plan(self, references):
        objects, recipes, assets = [], {}, set()
        for kind, object_id in references:
            if kind == "video" and object_id in self.recipes and not self.path(object_id).is_file():
                recipes[object_id] = self.recipes[object_id]
                assets.add(self.recipes[object_id]["asset"])
            else:
                objects.append({"kind": kind, "id": object_id})
        return {"objects": objects, "recipes": recipes, "assets": sorted(assets)}


def compact_store_class():
    """The real Store once it implements the contract, otherwise the test double."""
    return Store if real_store_is_compact() else FakeCompactStore
