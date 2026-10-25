"""Share project versions through a folder synced by an existing cloud client.

Layout inside the chosen folder (everything is write-once):

    vibemogging-<project id>/
        project.json                         project id and name
        objects/<kind>/<id[:2]>/<id>.z       zlib-compressed ordinary objects
        assets/<id[:2]>/<id>                 compressed source media, as imported
        recipes/<pack id>.json               {raw video id: recipe}, frames without RGB
        devices/<device>/device.json         device name
        devices/<device>/snapshot-<time>-<id>.json

A device publishes only files that are missing, then writes its snapshot last.
Snapshots list every file they need with its size, so a partially synced folder
is reported as incomplete and never changes the open project. Each computer
keeps its own state.json/index; nothing mutable is shared and the folder is not
used as a lock. Receiving creates `device/branch` refs; merging stays explicit.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import zlib

from .repository import SNAPSHOT_FORMAT, Repository, asset_present, asset_size, require_compact, valid_recipe
from .storage import UserError, atomic_write, canonical, digest, valid_id

CLOUD_FORMAT = 1
DEVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
CONFIG = "cloud.json"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def shared_write(path: Path, data: bytes):
    """Write-once file in the shared folder: hidden temp name, then atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def shared_copy(source: Path, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as stream:
            shutil.copyfileobj(stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def file_size(path: Path):
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


class CloudProject:
    """One project's shared directory plus this device's local settings."""

    def __init__(self, repo: Repository, folder: Path, device: str):
        self.repo = repo
        self.folder = Path(folder).expanduser().resolve()
        self.device = device
        project = repo.state["project"]
        if not project:
            raise UserError("Сначала импортируйте видео или откройте проект.")
        self.root = self.folder / f"vibemogging-{project['id']}"

    # ----- local configuration (this computer only) ---------------------------

    @staticmethod
    def config_path(repo: Repository) -> Path:
        return repo.root / CONFIG

    @classmethod
    def load(cls, repo: Repository):
        path = cls.config_path(repo)
        if not path.is_file() or not repo.state["project"]:
            return None
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            return cls(repo, Path(config["folder"]), config["device"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise UserError("Повреждены настройки облачной папки этого проекта (cloud.json).") from error

    @classmethod
    def connect(cls, repo: Repository, folder, device):
        if not repo.state["project"]:
            raise UserError("Сначала импортируйте видео или откройте проект.")
        if not isinstance(folder, str) or not folder.strip():
            raise UserError("Укажите путь к папке облачного клиента.")
        folder = Path(folder.strip()).expanduser()
        if not folder.is_absolute():
            raise UserError("Укажите полный путь к папке, например /Users/имя/Dropbox/Vibemogging.")
        if not folder.is_dir():
            raise UserError("Папка не найдена. Создайте её в папке облачного клиента и повторите.")
        device = str(device or "").strip()
        if not DEVICE_RE.fullmatch(device):
            raise UserError("Имя устройства: латинские буквы, цифры, «.», «_», «-»; до 40 символов.")
        cloud = cls(repo, folder, device)
        owner = cloud.device_owner(device)
        local_id = cloud.local_device_id()
        if owner and owner != local_id:
            raise UserError("Это имя устройства уже занято в общей папке другим компьютером. Выберите другое.")
        cloud.root.mkdir(parents=True, exist_ok=True)
        project_file = cloud.root / "project.json"
        if not project_file.exists():
            shared_write(project_file, canonical({"format": CLOUD_FORMAT, "project": repo.state["project"]}))
        else:
            cloud.check_project()
        device_file = cloud.root / "devices" / device / "device.json"
        if not device_file.exists():
            shared_write(device_file, canonical({"format": CLOUD_FORMAT, "name": device, "id": local_id,
                                                 "created": now()}))
        atomic_write(cls.config_path(repo), canonical({"folder": str(cloud.folder), "device": device,
                                                       "device_id": local_id}))
        return cloud

    def local_device_id(self):
        path = self.config_path(self.repo)
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("device_id")
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return uuid.uuid5(uuid.NAMESPACE_URL, str(self.repo.root)).hex

    def device_owner(self, device):
        try:
            info = json.loads((self.root / "devices" / device / "device.json").read_text(encoding="utf-8"))
            return info.get("id")
        except (OSError, ValueError, AttributeError):
            return None

    @classmethod
    def disconnect(cls, repo: Repository):
        cls.config_path(repo).unlink(missing_ok=True)

    def check_project(self):
        try:
            info = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise UserError("Файл project.json в общей папке ещё не синхронизирован или повреждён.") from error
        if info.get("project", {}).get("id") != self.repo.state["project"]["id"]:
            raise UserError("В общей папке находится другой проект.")

    # ----- shared paths --------------------------------------------------------

    def object_path(self, kind, object_id):
        if kind not in {"commit", "manifest", "video", "audio", "lut"} or not valid_id(object_id):
            raise UserError("Некорректный объект в снимке.")
        return self.root / "objects" / kind / object_id[:2] / f"{object_id}.z"

    def asset_path(self, asset_id):
        if not valid_id(asset_id):
            raise UserError("Некорректный исходный файл в снимке.")
        return self.root / "assets" / asset_id[:2] / asset_id

    def pack_path(self, pack_id):
        if not valid_id(pack_id):
            raise UserError("Некорректный список кадров в снимке.")
        return self.root / "recipes" / f"{pack_id}.json"

    # ----- publishing ------------------------------------------------------------

    def known_recipes(self):
        """Recipe IDs already present in complete packs (packs are immutable)."""
        known = {}
        directory = self.root / "recipes"
        if directory.is_dir():
            for path in directory.glob("*.json"):
                if not valid_id(path.stem):
                    continue
                try:
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != path.stem:
                        continue  # still syncing or damaged; not counted as present
                    for video_id in json.loads(data):
                        known.setdefault(video_id, path.stem)
                except (OSError, ValueError):
                    continue
        return known

    def publish(self, progress=lambda value: None) -> dict:
        self.check_project()
        repo, store = self.repo, self.repo.store
        store.flush()
        snapshot = repo.export_snapshot()
        files, copied, skipped, copied_bytes = {}, 0, 0, 0
        total = max(1, len(snapshot["objects"]) + len(snapshot["assets"]) + 1)
        done = 0
        for asset in snapshot["assets"]:
            target, size = self.asset_path(asset), asset_size(store, asset)
            if file_size(target) != size:
                shared_copy(store.asset_path(asset), target)
                copied, copied_bytes = copied + 1, copied_bytes + size
            else:
                skipped += 1
            files[str(target.relative_to(self.root))] = size
            done += 1
            progress(done / total)
        for item in snapshot["objects"]:
            target = self.object_path(item["kind"], item["id"])
            size = file_size(target)
            # Existing write-once files are trusted here; receivers verify every digest.
            if not size:
                data = zlib.compress(store.get(item["kind"], item["id"]), 6)
                shared_write(target, data)
                size = len(data)
                copied, copied_bytes = copied + 1, copied_bytes + size
            else:
                skipped += 1
            files[str(target.relative_to(self.root))] = size
            done += 1
            progress(done / total)
        known = self.known_recipes()
        missing = {video_id: recipe for video_id, recipe in snapshot["recipes"].items() if video_id not in known}
        packs = {known[video_id] for video_id in snapshot["recipes"] if video_id in known}
        if missing:
            data = canonical(missing)
            pack = hashlib.sha256(data).hexdigest()
            if file_size(self.pack_path(pack)) != len(data):
                shared_write(self.pack_path(pack), data)
                copied, copied_bytes = copied + 1, copied_bytes + len(data)
            packs.add(pack)
        for pack in packs:
            files[str(self.pack_path(pack).relative_to(self.root))] = file_size(self.pack_path(pack))
        created = now()
        record = {"format": CLOUD_FORMAT, "device": self.device, "created": created,
                  "snapshot": {key: snapshot[key] for key in ("format", "project", "branches", "head", "objects", "assets")},
                  "recipe_ids": sorted(snapshot["recipes"]), "packs": sorted(packs), "files": files}
        name = f"snapshot-{created.replace(':', '').replace('.', '')}-{uuid.uuid4().hex[:8]}.json"
        shared_write(self.root / "devices" / self.device / name, canonical(record))
        progress(1)
        return {"snapshot": name, "copied": copied, "skipped": skipped, "copied_bytes": copied_bytes,
                "branches": sorted(snapshot["branches"])}

    # ----- receiving -------------------------------------------------------------

    def snapshots(self):
        """Latest snapshot per other device, with completeness of the synced files."""
        self.check_project()
        devices = []
        directory = self.root / "devices"
        if not directory.is_dir():
            return devices
        for device_dir in sorted(directory.iterdir()):
            if not device_dir.is_dir() or device_dir.name == self.device or not DEVICE_RE.fullmatch(device_dir.name):
                continue
            names = sorted((p.name for p in device_dir.glob("snapshot-*.json")), reverse=True)
            info = {"device": device_dir.name, "snapshots": len(names), "latest": None}
            for name in names:
                record = self._read_record(device_dir / name)
                if record is None:
                    # Snapshot file itself is still syncing: try the previous one.
                    info.setdefault("unreadable", 0)
                    info["unreadable"] += 1
                    continue
                missing = [(path, size) for path, size in record["files"].items()
                           if file_size(self.root / path) != size]
                info["latest"] = {"file": name, "created": record["created"],
                                  "branches": record["snapshot"]["branches"],
                                  "complete": not missing, "missing_files": len(missing),
                                  "missing_bytes": sum(size or 0 for _, size in missing),
                                  "received": self._already_received(device_dir.name, record)}
                break
            devices.append(info)
        return devices

    def _read_record(self, path):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            snapshot = record["snapshot"]
            if (record.get("format") != CLOUD_FORMAT or snapshot.get("format") != SNAPSHOT_FORMAT or
                    not isinstance(record.get("files"), dict) or not isinstance(record.get("packs"), list) or
                    not isinstance(record.get("recipe_ids"), list) or not isinstance(snapshot.get("branches"), dict)):
                return None
            for relative in record["files"]:
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    return None
            return record
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _already_received(self, device, record):
        branches = self.repo.state["branches"]
        return all(branches.get(f"{device}/{name}") == ref for name, ref in record["snapshot"]["branches"].items())

    def receive(self, device, progress=lambda value: None) -> dict:
        self.check_project()
        if not isinstance(device, str) or not DEVICE_RE.fullmatch(device) or device == self.device:
            raise UserError("Выберите другое устройство из общей папки.")
        info = next((d for d in self.snapshots() if d["device"] == device), None)
        if not info or not info["latest"]:
            raise UserError("У этого устройства ещё нет полностью синхронизированных снимков.")
        latest = info["latest"]
        if not latest["complete"]:
            raise UserError(f"Облако ещё синхронизирует снимок: не хватает файлов — {latest['missing_files']}. "
                            "Подождите и повторите; текущий проект не изменён.")
        record = self._read_record(self.root / "devices" / device / latest["file"])
        if record is None:
            raise UserError("Снимок устройства повреждён или ещё синхронизируется.")
        return self._install(record, device, progress)

    def _install(self, record, peer, progress):
        repo, store = self.repo, self.repo.store
        snapshot = record["snapshot"]
        require_compact(store, bool(snapshot["assets"] or record["recipe_ids"]))
        assets = snapshot["assets"]
        total = max(1, len(assets) + len(snapshot["objects"]) + 1)
        done, fetched = 0, 0
        for asset in assets:
            source = self.asset_path(asset)
            size = file_size(source)
            if size is None:
                raise UserError("Исходный файл ещё не синхронизирован.")
            if not asset_present(store, asset):
                with source.open("rb") as stream:
                    store.import_asset(stream, asset, size)
                fetched += size
            done += 1
            progress(.5 * done / total)
        for item in snapshot["objects"]:
            kind, ref = item["kind"], item["id"]
            path = self.object_path(kind, ref)
            try:
                data = zlib.decompress(path.read_bytes())
            except (OSError, zlib.error) as error:
                raise UserError("Файл объекта ещё синхронизируется или повреждён. Повторите позже.") from error
            if digest(kind, data) != ref:
                raise UserError("Контрольная сумма объекта из общей папки не совпадает. Повторите позже.")
            store.put(kind, data)
            done += 1
            progress(.5 * done / total)
        wanted = set(record["recipe_ids"])
        recipes = {}
        for pack in record["packs"]:
            path = self.pack_path(pack)
            try:
                data = path.read_bytes()
            except OSError as error:
                raise UserError("Список кадров ещё синхронизируется.") from error
            if hashlib.sha256(data).hexdigest() != pack:
                raise UserError("Список кадров в общей папке повреждён или неполон.")
            for video_id, recipe in json.loads(data).items():
                if video_id in wanted:
                    recipes[video_id] = recipe
        if set(recipes) != wanted:
            raise UserError("В общей папке не хватает описаний кадров.")
        for video_id, recipe in recipes.items():
            if not valid_id(video_id) or not valid_recipe(recipe):
                raise UserError("Некорректное описание кадра в общей папке.")
            store.import_recipe(video_id, recipe)
        cloning = not repo.state["project"]
        repo.install_snapshot(dict(snapshot), peer, progress=lambda p: progress(.5 + .5 * p))
        names = sorted(snapshot["branches"])
        return {"device": peer, "branches": names if cloning else [f"{peer}/{name}" for name in names],
                "fetched_bytes": fetched}

    # ----- status ----------------------------------------------------------------

    def status(self):
        own = sorted((self.root / "devices" / self.device).glob("snapshot-*.json"), reverse=True)
        record = self._read_record(own[0]) if own else None
        return {"connected": True, "folder": str(self.folder), "device": self.device,
                "shared": str(self.root), "published": own[0].name if own else None,
                "published_at": record["created"] if record else None,
                "published_branches": record["snapshot"]["branches"] if record else None,
                "devices": self.snapshots()}


def list_shared_projects(folder) -> list:
    """Projects available in a cloud folder (for creating a local copy)."""
    folder = Path(str(folder or "").strip()).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise UserError("Папка не найдена. Укажите полный путь к папке облачного клиента.")
    projects = []
    for path in sorted(folder.glob("vibemogging-*")):
        try:
            info = json.loads((path / "project.json").read_text(encoding="utf-8"))["project"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        devices = sorted(p.name for p in (path / "devices").iterdir() if p.is_dir()) if (path / "devices").is_dir() else []
        projects.append({"id": info.get("id"), "name": info.get("name"), "devices": devices})
    return projects


def clone_from_cloud(repo: Repository, folder, project_id, source_device, device, progress=lambda value: None):
    """Fill an EMPTY local repository from another device's latest complete snapshot."""
    if repo.state["project"]:
        raise UserError("Получить общий проект можно только в новый пустой проект.")
    if not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{32}", project_id):
        raise UserError("Выберите проект из общей папки.")
    folder_path = Path(str(folder or "").strip()).expanduser()
    root = folder_path / f"vibemogging-{project_id}"
    try:
        info = json.loads((root / "project.json").read_text(encoding="utf-8"))["project"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise UserError("Проект в общей папке ещё не синхронизирован.") from error
    # A temporary project header lets the shared helpers address the folder.
    repo.state["project"] = dict(info)
    try:
        cloud = CloudProject(repo, folder_path, device)
        if device and cloud.device_owner(device):
            raise UserError("Это имя устройства уже используется в общей папке. Выберите другое.")
        latest = next((d["latest"] for d in cloud.snapshots() if d["device"] == source_device), None)
        if not latest or not latest["complete"]:
            raise UserError("Снимок выбранного устройства ещё не полностью синхронизирован.")
        record = cloud._read_record(root / "devices" / source_device / latest["file"])
        if record is None:
            raise UserError("Снимок устройства повреждён или ещё синхронизируется.")
    finally:
        repo.state["project"] = None
    result = cloud._install(record, "peer", progress)
    CloudProject.connect(repo, str(folder_path), device)
    return result
