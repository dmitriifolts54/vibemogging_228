"""Local commit graph, per-branch drafts, merge state, and portable snapshots."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache, wraps
import json
from pathlib import Path
import re
import shutil
import threading
import uuid
import zipfile

from .merge import (AUDIO_BLOCK, CHANNELS, FRAME_BYTES, SAMPLE_RATE, diff_manifests,
                    merge_manifests, read_audio, write_audio)
from .storage import Store, UserError, atomic_write, canonical, digest, valid_id

SNAPSHOT_FORMAT = 2
OBJECT_KINDS = {"commit", "manifest", "video", "audio", "lut"}
MAX_OBJECT = 64 * 1024 * 1024
MAX_INDEX = 512 * 1024 * 1024


def storage_plan(store: Store, references) -> dict:
    """Objects, recipes and assets needed for `references`, without decoding frames.

    Uses the compact Store's dependency expansion when present; a legacy Store
    only has ordinary objects, so the plan is the reference set itself.
    """
    references = sorted(set(references))
    if hasattr(store, "export_plan"):
        plan = store.export_plan(references)
        return {"objects": [{"kind": o["kind"], "id": o["id"]} for o in plan["objects"]],
                "recipes": dict(plan.get("recipes", {})), "assets": list(plan.get("assets", []))}
    return {"objects": [{"kind": kind, "id": ref} for kind, ref in references], "recipes": {}, "assets": []}


def store_has(store: Store, kind: str, object_id: str) -> bool:
    if hasattr(store, "has"):
        return store.has(kind, object_id)
    return store.path(object_id).is_file()


def asset_size(store: Store, asset_id: str) -> int:
    return store.asset_path(asset_id).stat().st_size


def asset_present(store: Store, asset_id: str) -> bool:
    try:
        return store.asset_path(asset_id).is_file()
    except (UserError, OSError):
        return False


def require_compact(store: Store, needs_assets: bool):
    if needs_assets and not all(hasattr(store, name) for name in ("import_asset", "import_recipe", "asset_path")):
        raise UserError("Этот файл использует компактное хранение исходников. Обновите приложение на этом компьютере.")


def valid_recipe(recipe) -> bool:
    if not isinstance(recipe, dict):
        return False
    if recipe.get("type") == "source":
        fps = recipe.get("fps")
        return (valid_id(recipe.get("asset")) and isinstance(recipe.get("frame"), int) and recipe["frame"] >= 0 and
                isinstance(fps, list) and len(fps) == 2 and all(isinstance(v, int) and v > 0 for v in fps))
    if recipe.get("type") == "lut":
        return valid_id(recipe.get("base")) and valid_id(recipe.get("table"))
    return False


def mutation(method):
    """Restore in-memory state if a mutation fails before its atomic save."""
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.lock:
            previous = deepcopy(self.state)
            try:
                return method(self, *args, **kwargs)
            except Exception as error:
                self.state = previous
                if isinstance(error, OSError):
                    raise UserError("Не удалось сохранить данные. Проверьте свободное место и доступ к папке репозитория.") from error
                raise
    return run


class Repository:
    def __init__(self, root: Path, store: Store | None = None):
        self.root = Path(root).resolve()
        self.store = store if store is not None else Store(self.root)
        self.lock = threading.RLock()
        self.pixel_resolver = None
        state_file = self.root / "state.json"
        if state_file.exists():
            try:
                self.state = json.loads(state_file.read_text(encoding="utf-8"))
                self._validate_state()
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise UserError("Не удалось прочитать state.json: повреждены метаданные репозитория. Сохраните копию папки данных для восстановления.") from error
        else:
            self.state = {"format": 1, "project": None, "head": "main", "branches": {},
                          "drafts": {}, "merge": None, "incoming": []}
        self.state.setdefault("incoming", [])

    def _validate_state(self):
        state = self.state
        if state["format"] != 1 or not isinstance(state["branches"], dict) or not isinstance(state["drafts"], dict):
            raise ValueError("Invalid repository state")
        if not isinstance(state["head"], str):
            raise ValueError("Invalid current branch")
        for name, ref in state["branches"].items():
            self.branch_name(name)
            if not valid_id(ref):
                raise ValueError("Invalid branch ref")
        for name, ref in state["drafts"].items():
            if name not in state["branches"] or not valid_id(ref):
                raise ValueError("Invalid draft")
        if state["project"] is None:
            if state["branches"] or state["drafts"] or state["merge"]:
                raise ValueError("Uninitialised repository has refs")
        elif (not isinstance(state["project"], dict) or state["head"] not in state["branches"] or
              not re.fullmatch(r"[0-9a-f]{32}", str(state["project"].get("id", ""))) or
              not isinstance(state["project"].get("name"), str)):
            raise ValueError("Invalid project")
        incoming = state.get("incoming", [])
        if not isinstance(incoming, list) or not all(isinstance(name, str) and name in state["branches"] for name in incoming):
            raise ValueError("Invalid incoming refs")
        pending = state["merge"]
        if pending is not None and (not isinstance(pending, dict) or
                                    not valid_id(pending.get("base")) or
                                    not isinstance(pending.get("parents"), list) or len(pending["parents"]) != 2 or
                                    not all(valid_id(ref) for ref in pending["parents"]) or
                                    not isinstance(pending.get("conflicts"), list)):
            raise ValueError("Invalid pending merge")

    def save(self):
        atomic_write(self.root / "state.json", canonical(self.state))

    @property
    def head(self):
        return self.state["branches"].get(self.state["head"])

    def get_commit(self, ref):
        commit_id = self.state["branches"].get(ref, ref)
        if not valid_id(commit_id):
            raise UserError("Версия или ветка не найдена.")
        return self.store.get_json("commit", commit_id)

    def manifest_id(self, ref="working"):
        if ref == "working":
            branch = self.state["head"]
            if branch in self.state["drafts"]:
                return self.state["drafts"][branch]
            if not self.head:
                raise UserError("Сначала импортируйте видео или репозиторий.")
            ref = self.head
        return self.get_commit(ref)["manifest"]

    def manifest(self, ref="working"):
        return self.store.get_json("manifest", self.manifest_id(ref))

    @mutation
    def set_working(self, manifest):
        self.state["drafts"][self.state["head"]] = self.store.put_json("manifest", manifest)
        self.save()

    def dirty(self):
        return bool(self.head and self.manifest_id() != self.get_commit(self.head)["manifest"])

    @mutation
    def initialise(self, manifest, name):
        if self.state["project"]:
            raise UserError("В этой папке уже есть проект. Для нового используйте другой --data-dir.")
        self.state["project"] = {"id": uuid.uuid4().hex, "name": str(name)[:200]}
        self.state["drafts"]["main"] = self.store.put_json("manifest", manifest)
        return self.commit("Первый импорт")

    @mutation
    def commit(self, message, author="Local editor"):
        message = str(message).strip()
        if not message:
            raise UserError("Добавьте комментарий к версии.")
        pending = self.state["merge"]
        if pending and any(c["choice"] is None for c in pending["conflicts"]):
            raise UserError("Сначала разрешите все конфликты слияния.")
        if self.head and not self.dirty() and not pending:
            raise UserError("Нет изменений для сохранения.")
        parents = pending["parents"] if pending else ([self.head] if self.head else [])
        commit = {"manifest": self.manifest_id(), "parents": parents, "message": message[:2000],
                  "author": str(author).strip()[:100] or "Local editor",
                  "created": datetime.now(timezone.utc).isoformat()}
        commit_id = self.store.put_json("commit", commit)
        self.state["branches"][self.state["head"]] = commit_id
        self.state["drafts"].pop(self.state["head"], None)
        self.state["merge"] = None
        self.save()
        return commit_id

    def assert_no_merge(self):
        if self.state["merge"]:
            raise UserError("Завершите или отмените текущее слияние.")

    def assert_clean(self):
        self.assert_no_merge()
        if self.dirty():
            raise UserError("Сохраните изменения в коммит перед слиянием или передачей репозитория.")

    @staticmethod
    def branch_name(name):
        name = str(name).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", name) or ".." in name or name.endswith("/"):
            raise UserError("Имя ветки: латинские буквы, цифры, дефис; до 80 символов.")
        if name == "working":
            raise UserError("Имя working зарезервировано.")
        return name

    @mutation
    def branch(self, name):
        self.assert_no_merge()
        if not self.head:
            raise UserError("Сначала импортируйте ролик.")
        name = self.branch_name(name)
        if name in self.state["branches"]:
            raise UserError("Ветка с таким именем уже существует.")
        draft = self.manifest_id()
        self.state["branches"][name] = self.head
        if self.dirty():
            self.state["drafts"][name] = draft
        self.state["head"] = name
        self.save()

    @mutation
    def checkout(self, name):
        self.assert_no_merge()
        if name not in self.state["branches"]:
            raise UserError("Ветка не найдена.")
        self.state["head"] = name
        self.save()

    @mutation
    def restore(self, ref):
        self.assert_no_merge()
        self.state["drafts"][self.state["head"]] = self.manifest_id(ref)
        self.save()

    @mutation
    def discard(self):
        self.assert_no_merge()
        self.state["drafts"].pop(self.state["head"], None)
        self.save()

    def ancestors(self, commit_id):
        found, queue = set(), [commit_id]
        while queue:
            current = queue.pop()
            if current not in found:
                found.add(current)
                queue.extend(self.get_commit(current)["parents"])
        return found

    def merge_base(self, ours, theirs):
        common = self.ancestors(ours) & self.ancestors(theirs)
        if not common:
            raise UserError("У веток нет общей исходной версии.")
        non_tips = {parent for node in common for parent in self.get_commit(node)["parents"]}
        best = common - non_tips
        if len(best) != 1:
            raise UserError("Несколько общих баз слияния: такой граф пока не поддерживается в V1.")
        return best.pop()

    @mutation
    def merge(self, source):
        self.assert_clean()
        if source not in self.state["branches"]:
            raise UserError("Выберите существующую ветку для слияния.")
        ours, theirs = self.head, self.state["branches"][source]
        if theirs in self.ancestors(ours):
            return {"mode": "unchanged", "message": "Изменения этой ветки уже включены."}
        if ours in self.ancestors(theirs):
            self.state["branches"][self.state["head"]] = theirs
            self.state["drafts"].pop(self.state["head"], None)
            self.save()
            return {"mode": "fast-forward", "message": "Ветка обновлена до выбранной версии."}
        base = self.merge_base(ours, theirs)
        merged, conflicts = merge_manifests(self.store, self.manifest(base), self.manifest(ours),
                                            self.manifest(theirs), self.pixel_resolver)
        self.state["merge"] = {"base": base, "parents": [ours, theirs], "source": source,
                               "conflicts": conflicts}
        self.state["drafts"][self.state["head"]] = self.store.put_json("manifest", merged)
        if not conflicts:
            commit = self.commit(f"Слияние {source} → {self.state['head']}")
            return {"mode": "merged", "commit": commit, "message": "Изменения объединены без конфликтов."}
        self.save()
        return {"mode": "conflicts", "message": f"Нужно разрешить конфликтов: {len(conflicts)}."}

    @mutation
    def resolve(self, conflict_id, choice):
        pending = self.state["merge"]
        if not pending or choice not in {"base", "ours", "theirs"}:
            raise UserError("Выберите исходник, нашу или входящую версию.")
        conflict = next((c for c in pending["conflicts"] if c["id"] == conflict_id), None)
        if not conflict:
            raise UserError("Конфликт не найден.")
        source_ref = {"base": pending["base"], "ours": pending["parents"][0],
                      "theirs": pending["parents"][1]}[choice]
        source, result = self.manifest(source_ref), self.manifest()
        start, end = conflict["start"], conflict["end"]
        if conflict["kind"] == "video":
            result["video"][start:end] = source["video"][start:end]
        else:
            original = self.manifest(pending["base"])
            ours = self.manifest(pending["parents"][0])
            theirs = self.manifest(pending["parents"][1])
            for begin in range(start * 2, end * 2, AUDIO_BLOCK * FRAME_BYTES):
                finish = min(end * 2, begin + AUDIO_BLOCK * FRAME_BYTES)
                base_data, left_data, right_data = (
                    read_audio(self.store, manifest, begin, finish) for manifest in (original, ours, theirs))
                selected = {"base": base_data, "ours": left_data, "theirs": right_data}[choice]
                data = bytearray(read_audio(self.store, result, begin, finish))
                for offset in range(0, len(data), 2):
                    bv, av, cv = (chunk[offset:offset + 2] for chunk in (base_data, left_data, right_data))
                    if av != cv and av != bv and cv != bv:
                        data[offset:offset + 2] = selected[offset:offset + 2]
                write_audio(self.store, result, begin, bytes(data))
        conflict["choice"] = choice
        self.state["drafts"][self.state["head"]] = self.store.put_json("manifest", result)
        self.save()

    @mutation
    def cancel_merge(self):
        if not self.state["merge"]:
            raise UserError("Сейчас нет незавершённого слияния.")
        self.state["drafts"].pop(self.state["head"], None)
        self.state["merge"] = None
        self.save()

    @lru_cache(maxsize=64)
    def _diff(self, left_id, right_id):
        return diff_manifests(self.store, self.store.get_json("manifest", left_id),
                              self.store.get_json("manifest", right_id))

    def diff(self, left, right="working"):
        return self._diff(self.manifest_id(left), self.manifest_id(right))

    def history(self):
        ids = set()
        for commit_id in self.state["branches"].values():
            ids.update(self.ancestors(commit_id))
        output = []
        for commit_id in ids:
            record = {**self.get_commit(commit_id), "id": commit_id,
                      "branches": [name for name, head in self.state["branches"].items() if head == commit_id]}
            output.append(record)
        return sorted(output, key=lambda c: c["created"], reverse=True)

    def status(self):
        if not self.state["project"]:
            return {"project": None}
        manifest = self.manifest()
        fps = manifest["fps"][0] / manifest["fps"][1]
        pending = deepcopy(self.state["merge"])
        if pending:
            for conflict in pending["conflicts"]:
                scale = fps if conflict["kind"] == "video" else SAMPLE_RATE * CHANNELS
                conflict["start_time"], conflict["end_time"] = conflict["start"] / scale, conflict["end"] / scale
        return {"project": self.state["project"], "branch": self.state["head"],
                "branches": self.state["branches"], "drafts": list(self.state["drafts"]),
                "incoming": self.state["incoming"],
                "head": self.head, "dirty": self.dirty(), "merge": pending,
                "manifest": self.manifest_id(), "width": manifest["width"], "height": manifest["height"],
                "fps": fps, "frames": len(manifest["video"]), "duration": len(manifest["video"]) / fps,
                "history": self.history(), "diff": self.diff(self.head)}

    # ----- portable snapshots -------------------------------------------------

    def local_branches(self):
        return {name: ref for name, ref in self.state["branches"].items() if name not in self.state["incoming"]}

    def export_snapshot(self) -> dict:
        """Committed local branches plus the storage plan; drafts stay on this device."""
        if not self.head:
            raise UserError("Сначала создайте проект.")
        branches = self.local_branches()
        if not branches:
            raise UserError("Создайте локальную ветку перед передачей репозитория.")
        reachable = set()
        for ref in branches.values():
            reachable.update(self.ancestors(ref))
        references = set()
        for commit_id in reachable:
            commit = self.get_commit(commit_id)
            references.add(("commit", commit_id))
            references.add(("manifest", commit["manifest"]))
            manifest = self.store.get_json("manifest", commit["manifest"])
            references.update(("video", ref) for ref in manifest["video"])
            references.update(("audio", ref) for ref in manifest["audio"])
        head = self.state["head"] if self.state["head"] in branches else next(iter(branches))
        return {"format": SNAPSHOT_FORMAT, "project": dict(self.state["project"]),
                "branches": branches, "head": head, **storage_plan(self.store, references)}

    def export_bundle(self, output: Path, progress=lambda value: None):
        """Write a `.vmrepo` format 2: objects/<kind>/<id>, assets/<id> (stored), recipes.json."""
        self.assert_clean()
        snapshot = self.export_snapshot()
        index = {key: snapshot[key] for key in ("format", "project", "branches", "head", "objects")}
        index["assets"] = {asset: asset_size(self.store, asset) for asset in snapshot["assets"]}
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".partial")
        total = max(1, len(index["objects"]) + len(index["assets"]))
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3,
                                 allowZip64=True) as archive:
                archive.writestr("index.json", canonical(index))
                archive.writestr("recipes.json", canonical(snapshot["recipes"]))
                done = 0
                for asset in sorted(index["assets"]):
                    # Source media is already compressed: store it as-is, streamed.
                    info = zipfile.ZipInfo(f"assets/{asset}")
                    info.compress_type = zipfile.ZIP_STORED
                    with self.store.asset_path(asset).open("rb") as source, \
                            archive.open(info, "w", force_zip64=True) as target:
                        shutil.copyfileobj(source, target, 1024 * 1024)
                    done += 1
                    progress(done / total)
                for item in sorted(index["objects"], key=lambda o: (o["kind"], o["id"])):
                    archive.writestr(f"objects/{item['kind']}/{item['id']}", self.store.get(item["kind"], item["id"]))
                    done += 1
                    progress(done / total)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    def import_bundle(self, path: Path, peer="peer", progress=lambda value: None):
        """Stage a V1 or V2 `.vmrepo` into the Store; refs change only after full validation."""
        with self.lock:
            self.assert_no_merge()
            try:
                archive = zipfile.ZipFile(path)
            except (zipfile.BadZipFile, OSError) as error:
                raise UserError("Не удалось открыть файл репозитория .vmrepo.") from error
            with archive:
                names = set(archive.namelist())
                if "index.json" not in names or archive.getinfo("index.json").file_size > MAX_INDEX:
                    raise UserError("Некорректный индекс репозитория.")
                try:
                    index = json.loads(archive.read("index.json"))
                except ValueError as error:
                    raise UserError("Некорректный индекс репозитория.") from error
                if not isinstance(index, dict) or index.get("format") not in {1, SNAPSHOT_FORMAT}:
                    raise UserError("Неподдерживаемый формат репозитория. Обновите приложение.")
                self._check_snapshot_header(index, self.branch_name(peer))
                try:
                    if index["format"] == 1:
                        snapshot = self._stage_v1(archive, index, progress)
                    else:
                        snapshot = self._stage_v2(archive, index, names, progress)
                except OSError as error:
                    raise UserError("Не удалось записать данные проекта. Проверьте свободное место.") from error
                except (zipfile.BadZipFile, KeyError) as error:
                    raise UserError("Файл репозитория повреждён или неполон.") from error
            self.install_snapshot(snapshot, peer, progress=lambda p: progress(.5 + .5 * p))

    def _stage_v1(self, archive, index, progress):
        objects = []
        items = [item for item in archive.infolist() if item.filename != "index.json"]
        seen = set()
        for number, item in enumerate(items):
            parts = item.filename.split("/")
            if (len(parts) != 3 or parts[0] != "objects" or parts[1] not in {"commit", "manifest", "video", "audio"}
                    or not valid_id(parts[2]) or item.file_size > MAX_OBJECT):
                raise UserError("Недопустимый объект в файле репозитория.")
            if (parts[1], parts[2]) in seen:
                raise UserError("Повторяющийся объект в репозитории.")
            seen.add((parts[1], parts[2]))
            self._put_verified(parts[1], parts[2], archive.read(item))
            objects.append({"kind": parts[1], "id": parts[2]})
            progress(.5 * (number + 1) / max(1, len(items)))
        return {"format": 1, "project": index["project"], "branches": index["branches"],
                "head": index.get("head"), "objects": objects, "recipes": {}, "assets": []}

    def _stage_v2(self, archive, index, names, progress):
        objects, assets = index.get("objects"), index.get("assets")
        if not isinstance(objects, list) or not isinstance(assets, dict) or "recipes.json" not in names:
            raise UserError("Некорректный индекс репозитория.")
        if archive.getinfo("recipes.json").file_size > MAX_INDEX:
            raise UserError("Слишком большой список кадров в файле репозитория.")
        try:
            recipes = json.loads(archive.read("recipes.json"))
        except ValueError as error:
            raise UserError("Повреждён список кадров в файле репозитория.") from error
        if not isinstance(recipes, dict):
            raise UserError("Повреждён список кадров в файле репозитория.")
        require_compact(self.store, bool(assets or recipes))
        expected = {f"assets/{a}" for a in assets} | {f"objects/{o.get('kind')}/{o.get('id')}" for o in objects
                                                        if isinstance(o, dict)}
        if len(expected) != len(assets) + len(objects) or not expected <= names:
            raise UserError("В файле репозитория не хватает данных или есть повторы.")
        total = max(1, len(assets) + len(objects) + len(recipes))
        done = 0
        for asset, size in assets.items():
            if not valid_id(asset) or not isinstance(size, int) or size < 0:
                raise UserError("Некорректное описание исходного файла.")
            if archive.getinfo(f"assets/{asset}").file_size != size:
                raise UserError("Исходный файл в репозитории повреждён или неполон.")
            if not asset_present(self.store, asset):
                with archive.open(f"assets/{asset}") as stream:
                    self.store.import_asset(stream, asset, size)
            done += 1
            progress(.5 * done / total)
        for item in objects:
            kind, ref = item["kind"], item["id"]
            if kind not in OBJECT_KINDS or not valid_id(ref) or archive.getinfo(f"objects/{kind}/{ref}").file_size > MAX_OBJECT:
                raise UserError("Недопустимый объект в файле репозитория.")
            self._put_verified(kind, ref, archive.read(f"objects/{kind}/{ref}"))
            done += 1
            progress(.5 * done / total)
        self._install_recipes(recipes)
        return {"format": SNAPSHOT_FORMAT, "project": index["project"], "branches": index["branches"],
                "head": index.get("head"), "objects": objects, "recipes": recipes, "assets": list(assets)}

    def _put_verified(self, kind, ref, data):
        if digest(kind, data) != ref:
            raise UserError("Контрольная сумма входящего объекта не совпадает.")
        self.store.put(kind, data)

    def _install_recipes(self, recipes):
        for video_id, recipe in recipes.items():
            if not valid_id(video_id) or not valid_recipe(recipe):
                raise UserError("Некорректное описание кадра в репозитории.")
            self.store.import_recipe(video_id, recipe)

    def _check_snapshot_header(self, index, peer):
        project = index.get("project")
        if not isinstance(project, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(project.get("id", ""))):
            raise UserError("Некорректный идентификатор проекта.")
        if self.state["project"] and self.state["project"]["id"] != project["id"]:
            raise UserError("Это другой видеопроект. Откройте его как отдельный проект.")
        branches = index.get("branches")
        if not branches or not isinstance(branches, dict):
            raise UserError("В репозитории нет веток.")
        for name, ref in branches.items():
            self.branch_name(name)
            if not valid_id(ref):
                raise UserError("Повреждена ссылка на версию.")
        if self.state["project"]:
            if self.state["head"] in {f"{peer}/{name}" for name in branches}:
                raise UserError("Переключитесь с входящей ветки перед её обновлением.")
            for name in branches:
                target = self.branch_name(f"{peer}/{name}")
                if target in self.state["branches"] and target not in self.state["incoming"]:
                    raise UserError("Входящая ветка совпадает с локальной. Выберите другое имя участника.")

    @mutation
    def install_snapshot(self, index: dict, peer="peer", progress=lambda value: None, verify_frames=True):
        """Activate a snapshot whose objects/recipes/assets are already staged in the Store.

        Validates the commit graph and every referenced frame/sample before any ref
        changes. Clones into an empty repository; otherwise adds `peer/BRANCH` refs.
        """
        self.assert_no_merge()
        peer = self.branch_name(peer)
        if not isinstance(index, dict) or index.get("format") not in {1, SNAPSHOT_FORMAT}:
            raise UserError("Неподдерживаемый формат снимка.")
        self._check_snapshot_header(index, peer)
        project, branches = index["project"], index["branches"]
        self._validate_graph(branches, progress if verify_frames else None)
        if not self.state["project"]:
            self.state["project"] = {"id": project["id"], "name": str(project.get("name", "Видео"))[:200]}
            self.state["branches"] = dict(branches)
            self.state["head"] = index.get("head") if index.get("head") in branches else next(iter(branches))
        else:
            for name, ref in branches.items():
                target = self.branch_name(f"{peer}/{name}")
                self.state["branches"][target] = ref
                if target not in self.state["incoming"]:
                    self.state["incoming"].append(target)
        self.save()

    def _validate_graph(self, branches, progress=None):
        seen, queue, manifests = set(), list(branches.values()), []
        while queue:
            ref = queue.pop()
            if ref in seen:
                continue
            seen.add(ref)
            if not store_has(self.store, "commit", ref):
                raise UserError("В полученных данных не хватает коммитов.")
            commit = self.store.get_json("commit", ref)
            if not isinstance(commit, dict):
                raise UserError("Повреждено описание коммита.")
            manifest_id = commit.get("manifest")
            if (not valid_id(manifest_id) or not store_has(self.store, "manifest", manifest_id) or
                    not isinstance(commit.get("parents"), list) or
                    not all(valid_id(parent) for parent in commit["parents"])):
                raise UserError("Повреждено описание версии.")
            for key in ("created", "message", "author"):
                if not isinstance(commit.get(key), str):
                    raise UserError("Повреждены метаданные коммита.")
            queue.extend(commit["parents"])
            manifests.append(manifest_id)
        checked = set()
        work = []
        for manifest_id in dict.fromkeys(manifests):
            manifest = self.store.get_json("manifest", manifest_id)
            try:
                width, height = manifest["width"], manifest["height"]
                numerator, denominator = manifest["fps"]
                if (not isinstance(width, int) or not isinstance(height, int) or
                        width < 2 or height < 2 or width % 2 or height % 2 or width * height > 3840 * 2160 or
                        not isinstance(numerator, int) or not isinstance(denominator, int) or
                        numerator <= 0 or denominator <= 0 or numerator / denominator > 240 or not manifest["video"]):
                    raise ValueError
                samples = round(len(manifest["video"]) * denominator * SAMPLE_RATE / numerator)
                if samples != manifest["samples"] or len(manifest["audio"]) != (samples + AUDIO_BLOCK - 1) // AUDIO_BLOCK:
                    raise ValueError
                for kind in ("video", "audio"):
                    for i, object_id in enumerate(manifest[kind]):
                        if not valid_id(object_id) or not store_has(self.store, kind, object_id):
                            raise ValueError
                        if (kind, object_id) not in checked:
                            checked.add((kind, object_id))
                            size = width * height * 3 if kind == "video" else min(AUDIO_BLOCK, samples - i * AUDIO_BLOCK) * FRAME_BYTES
                            work.append((kind, object_id, size))
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                raise UserError("Повреждена структура медиаданных или не хватает кадров.") from error
        if progress is None:
            return
        # Store.get re-checks content IDs, including frames rebuilt from source/LUT recipes.
        for number, (kind, object_id, size) in enumerate(work):
            if len(self.store.get(kind, object_id)) != size:
                raise UserError("Размер кадра или звукового блока не совпадает с проектом.")
            if number % 64 == 0:
                progress(number / len(work))
        progress(1)
