"""Local list of project folders for the start screen.

`workspace.json` belongs to this computer only: it is never placed in a cloud
folder. Every project is an ordinary data directory, so `--data-dir` keeps
working for single-project use and a project can still be moved by copying it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .storage import UserError, atomic_write, canonical

NAME_LIMIT = 120


def now():
    return datetime.now(timezone.utc).isoformat()


def folder_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def clean_name(name) -> str:
    name = " ".join(str(name or "").split())[:NAME_LIMIT]
    if not name:
        raise UserError("Введите название проекта.")
    return name


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.projects_dir = self.root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "workspace.json"
        if self.file.exists():
            try:
                self.state = json.loads(self.file.read_text(encoding="utf-8"))
                if not isinstance(self.state.get("projects"), list):
                    raise ValueError
            except (ValueError, AttributeError) as error:
                raise UserError("Не удалось прочитать workspace.json: список проектов повреждён.") from error
        else:
            self.state = {"format": 1, "projects": []}

    def save(self):
        atomic_write(self.file, canonical(self.state))

    def _entry(self, project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise UserError("Некорректный идентификатор проекта.")
        entry = next((p for p in self.state["projects"] if p["id"] == project_id), None)
        if entry is None:
            raise UserError("Проект не найден в списке.")
        return entry

    def path(self, project_id) -> Path:
        return Path(self._entry(project_id)["path"])

    def entry(self, project_id) -> dict:
        return dict(self._entry(project_id))

    def create(self, name, path: Path | None = None) -> dict:
        """Register a new empty project folder (inside the workspace unless `path` is given)."""
        project_id = uuid.uuid4().hex
        target = Path(path).expanduser().resolve() if path else self.projects_dir / project_id
        if target.exists() and any(target.iterdir()):
            raise UserError("Папка нового проекта уже существует и не пуста.")
        target.mkdir(parents=True, exist_ok=True)
        entry = {"id": project_id, "name": clean_name(name), "path": str(target),
                 "created": now(), "opened": None, "storage": None}
        self.state["projects"].append(entry)
        self.save()
        return dict(entry)

    def register(self, path: Path, name=None) -> dict:
        """Add an existing data folder (for example a V1 `.vibemogging`) without moving it."""
        target = Path(path).expanduser().resolve()
        if not (target / "state.json").is_file():
            raise UserError("В этой папке нет проекта Vibemogging (state.json не найден).")
        existing = next((p for p in self.state["projects"] if Path(p["path"]) == target), None)
        if existing:
            return dict(existing)
        if name is None:
            try:
                project = json.loads((target / "state.json").read_text(encoding="utf-8")).get("project") or {}
                name = project.get("name") or target.name
            except (OSError, ValueError, AttributeError):
                name = target.name
        entry = {"id": uuid.uuid4().hex, "name": clean_name(name), "path": str(target),
                 "created": now(), "opened": None, "storage": {"folder_bytes": folder_size(target)}}
        self.state["projects"].append(entry)
        self.save()
        return dict(entry)

    def rename(self, project_id, name):
        self._entry(project_id)["name"] = clean_name(name)
        self.save()

    def forget(self, project_id):
        """Remove a project from the list only; its folder is left untouched."""
        entry = self._entry(project_id)
        self.state["projects"].remove(entry)
        self.save()

    def touch(self, project_id, storage: dict | None = None):
        entry = self._entry(project_id)
        entry["opened"] = now()
        if storage is not None:
            entry["storage"] = storage
        self.save()

    def update_storage(self, project_id, storage: dict):
        self._entry(project_id)["storage"] = storage
        self.save()

    def listing(self):
        output = []
        for entry in sorted(self.state["projects"], key=lambda p: p.get("opened") or p["created"], reverse=True):
            path = Path(entry["path"])
            item = dict(entry)
            item["available"] = path.is_dir()
            item["initialised"] = False
            item["video_name"] = None
            if item["available"] and (path / "state.json").is_file():
                try:
                    project = json.loads((path / "state.json").read_text(encoding="utf-8")).get("project")
                    item["initialised"] = bool(project)
                    item["video_name"] = project.get("name") if project else None
                except (OSError, ValueError, AttributeError):
                    item["initialised"] = None
            item["cloud"] = (path / "cloud.json").is_file()
            # V1 layout: frame objects but no recipe index of the compact Store.
            item["legacy"] = bool(item["initialised"]) and not (path / "sources.sqlite3").exists()
            output.append(item)
        return output
