"""Local commit graph, per-branch drafts, merge state, and portable bundles."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
import threading
import uuid
import zipfile

from .merge import (AUDIO_BLOCK, CHANNELS, FRAME_BYTES, SAMPLE_RATE, diff_manifests,
                    merge_manifests, read_audio, write_audio)
from .storage import Store, UserError, atomic_write, canonical, digest, valid_id


class Repository:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.store = Store(self.root)
        self.lock = threading.RLock()
        self.pixel_resolver = None
        state_file = self.root / "state.json"
        if state_file.exists():
            self.state = json.loads(state_file.read_text())
        else:
            self.state = {"format": 1, "project": None, "head": "main", "branches": {},
                          "drafts": {}, "merge": None}

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

    def set_working(self, manifest):
        self.state["drafts"][self.state["head"]] = self.store.put_json("manifest", manifest)
        self.save()

    def dirty(self):
        return bool(self.head and self.manifest_id() != self.get_commit(self.head)["manifest"])

    def initialise(self, manifest, name):
        if self.state["project"]:
            raise UserError("В этой папке уже есть проект. Для нового используйте другой --data-dir.")
        self.state["project"] = {"id": uuid.uuid4().hex, "name": str(name)[:200]}
        self.state["drafts"]["main"] = self.store.put_json("manifest", manifest)
        return self.commit("Первый импорт")

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

    def checkout(self, name):
        self.assert_no_merge()
        if name not in self.state["branches"]:
            raise UserError("Ветка не найдена.")
        self.state["head"] = name
        self.save()

    def restore(self, ref):
        self.assert_no_merge()
        self.state["drafts"][self.state["head"]] = self.manifest_id(ref)
        self.save()

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
            data = read_audio(self.store, source, start * 2, end * 2)
            write_audio(self.store, result, start * 2, data)
        conflict["choice"] = choice
        self.state["drafts"][self.state["head"]] = self.store.put_json("manifest", result)
        self.save()

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
                "head": self.head, "dirty": self.dirty(), "merge": pending,
                "manifest": self.manifest_id(), "width": manifest["width"], "height": manifest["height"],
                "fps": fps, "frames": len(manifest["video"]), "duration": len(manifest["video"]) / fps,
                "history": self.history(), "diff": self.diff(self.head)}

    def export_bundle(self, output: Path):
        self.assert_clean()
        if not self.head:
            raise UserError("Сначала создайте проект.")
        objects = set()
        for commit in self.history():
            objects.add(("commit", commit["id"]))
            objects.add(("manifest", commit["manifest"]))
            manifest = self.store.get_json("manifest", commit["manifest"])
            objects.update(("video", ref) for ref in manifest["video"])
            objects.update(("audio", ref) for ref in manifest["audio"])
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".partial")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                archive.writestr("index.json", canonical({"format": 1, "project": self.state["project"],
                                                          "branches": self.state["branches"],
                                                          "head": self.state["head"]}))
                for kind, object_id in sorted(objects):
                    archive.writestr(f"objects/{kind}/{object_id}", self.store.get(kind, object_id))
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    def import_bundle(self, path: Path, peer="peer"):
        self.assert_no_merge()
        peer = self.branch_name(peer)
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as error:
            raise UserError("Не удалось открыть файл репозитория .vmrepo.") from error
        with archive:
            if "index.json" not in archive.namelist() or archive.getinfo("index.json").file_size > 1024 * 1024:
                raise UserError("Некорректный индекс репозитория.")
            index = json.loads(archive.read("index.json"))
            if index.get("format") != 1 or not isinstance(index.get("project"), dict):
                raise UserError("Неподдерживаемый формат репозитория.")
            project = index["project"]
            if not re.fullmatch(r"[0-9a-f]{32}", str(project.get("id", ""))):
                raise UserError("Некорректный идентификатор проекта.")
            if self.state["project"] and self.state["project"]["id"] != project["id"]:
                raise UserError("Это другой видеопроект. Откройте его с отдельным --data-dir.")
            branches = index.get("branches", {})
            if not branches or not isinstance(branches, dict):
                raise UserError("В репозитории нет веток.")
            for name, ref in branches.items():
                self.branch_name(name)
                if not valid_id(ref):
                    raise UserError("Повреждена ссылка на версию.")
            if self.state["project"] and self.state["head"] in {f"{peer}/{name}" for name in branches}:
                raise UserError("Переключитесь с входящей ветки перед её обновлением.")
            known = set()
            for item in archive.infolist():
                if item.filename == "index.json":
                    continue
                parts = item.filename.split("/")
                if (len(parts) != 3 or parts[0] != "objects" or
                    parts[1] not in {"commit", "manifest", "video", "audio"} or not valid_id(parts[2]) or
                    item.file_size > 32 * 1024 * 1024):
                    raise UserError("Недопустимый объект в файле репозитория.")
                kind, ref = parts[1:]
                if (kind, ref) in known:
                    raise UserError("Повторяющийся объект в репозитории.")
                data = archive.read(item)
                if digest(kind, data) != ref:
                    raise UserError("Контрольная сумма входящего объекта не совпадает.")
                self.store.put(kind, data)
                known.add((kind, ref))
            self._validate_bundle(branches, known)
        if not self.state["project"]:
            self.state["project"] = {"id": project["id"], "name": str(project.get("name", "Видео"))[:200]}
            self.state["branches"] = branches
            self.state["head"] = index.get("head") if index.get("head") in branches else next(iter(branches))
        else:
            for name, ref in branches.items():
                self.state["branches"][self.branch_name(f"{peer}/{name}")] = ref
        self.save()

    def _validate_bundle(self, branches, known):
        seen, queue = set(), list(branches.values())
        while queue:
            ref = queue.pop()
            if ref in seen:
                continue
            seen.add(ref)
            if ("commit", ref) not in known:
                raise UserError("В файле не хватает коммитов.")
            commit = self.store.get_json("commit", ref)
            manifest_id = commit.get("manifest")
            if ("manifest", manifest_id) not in known or not isinstance(commit.get("parents"), list):
                raise UserError("Повреждено описание версии.")
            for key in ("created", "message", "author"):
                if not isinstance(commit.get(key), str):
                    raise UserError("Повреждены метаданные коммита.")
            queue.extend(commit["parents"])
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
                        if (kind, object_id) not in known:
                            raise ValueError
                        expected = width * height * 3 if kind == "video" else min(AUDIO_BLOCK, samples - i * AUDIO_BLOCK) * FRAME_BYTES
                        if len(self.store.get(kind, object_id)) != expected:
                            raise ValueError
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                raise UserError("Повреждена структура медиаданных.") from error
