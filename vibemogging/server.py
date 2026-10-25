"""Loopback-only HTTP application and serialized background media jobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
from pathlib import Path
import os
import re
import shutil
import threading
import traceback
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import wave

from . import maintenance, media
from .cloud import CloudProject, clone_from_cloud, list_shared_projects
from .locking import DirectoryLock
from .merge import CHANNELS, FRAME_BYTES, SAMPLE_RATE, read_audio
from .repository import Repository
from .storage import Store, UserError, canonical, valid_id
from .workspace import Workspace, folder_size

MAX_UPLOAD = 4 * 1024 ** 3
STATIC = Path(__file__).parent / "static"
WORKSPACE_ACTIONS = {"project_create", "project_open", "project_close", "project_forget",
                     "project_register", "project_migrate", "project_clone_cloud"}


DEFAULT_PREVIEW_CACHE = 1024 * 1024 * 1024


def trim_preview_cache(directory: Path, limit: int, keep=()) -> dict:
    """Disposable MP4 previews/exports: drop least recently used files above `limit` bytes.

    Previews are rebuilt from the canonical history on demand, so evicting them
    never loses data. Files in `keep` (current working preview, the file just
    rendered) are never removed.
    """
    files = []
    for path in directory.glob("*.mp4"):
        try:
            files.append((path.stat().st_mtime, path.stat().st_size, path))
        except OSError:
            continue
    total = sum(size for _, size, _ in files)
    removed = 0
    for _, size, path in sorted(files):
        if total <= limit:
            break
        if path.name in keep:
            continue
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
    return {"preview_bytes": total, "preview_files": len(files) - removed, "preview_limit": limit,
            "preview_removed": removed}


def store_stats(repo: Repository):
    if hasattr(repo.store, "stats"):
        try:
            stats = dict(repo.store.stats())
            stats["folder_bytes"] = folder_size(repo.root)
            return stats
        except Exception:
            pass
    return {"folder_bytes": folder_size(repo.root)}


class App:
    """One server process: either a single `--data-dir` project or a workspace of projects."""

    def __init__(self, root: Path | None = None, workspace: Workspace | None = None, store_factory=Store,
                 preview_cache=DEFAULT_PREVIEW_CACHE):
        if (root is None) == (workspace is None):
            raise ValueError("Pass exactly one of root or workspace")
        self.workspace = workspace
        self.store_factory = store_factory
        self.preview_cache = preview_cache
        self.workspace_lock = DirectoryLock(workspace.root) if workspace else None
        self.repo = self.root = self.directory_lock = self.entry = None
        self.storage = self.preview_stats = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media")
        self.jobs = {}
        self.job_lock = threading.Lock()
        self.active = None
        self.snapshot = {"project": None, "ffmpeg": media.tools_available(), "busy": False}
        try:
            if workspace:
                (workspace.root / "uploads").mkdir(exist_ok=True)
            else:
                self.open_project(Path(root))
        except Exception:
            self.close()
            raise
        self.status()

    # ----- project lifecycle ------------------------------------------------------

    def open_project(self, path: Path, entry: dict | None = None):
        lock = DirectoryLock(Path(path))
        try:
            repo = Repository(path, store=self.store_factory(Path(path).resolve()))
            for directory in ("uploads", "preview", "bundles"):
                (repo.root / directory).mkdir(exist_ok=True)
        except Exception:
            lock.close()
            raise
        self.repo, self.root, self.directory_lock, self.entry = repo, repo.root, lock, entry
        self.storage = store_stats(repo)
        self.preview_stats = trim_preview_cache(repo.root / "preview", self.preview_cache)
        if self.workspace and entry:
            self.workspace.touch(entry["id"], self.storage)

    def close_project(self):
        if self.repo is None:
            return
        if self.workspace and self.entry:
            try:
                self.workspace.update_storage(self.entry["id"], store_stats(self.repo))
            except UserError:
                pass
        if hasattr(self.repo.store, "close"):
            self.repo.store.close()
        self.directory_lock.close()
        self.repo = self.root = self.directory_lock = self.entry = None
        self.storage = self.preview_stats = None
        with self.job_lock:
            self.snapshot = {"project": None, "ffmpeg": media.tools_available(), "busy": self.active is not None}

    def close(self):
        self.executor.shutdown(wait=True)
        self.close_project()
        if self.workspace_lock:
            self.workspace_lock.close()

    def busy(self):
        with self.job_lock:
            return self.active is not None

    def require_repo(self):
        if self.repo is None:
            raise UserError("Сначала откройте проект.")
        return self.repo

    @property
    def upload_dir(self):
        return (self.root if self.repo else self.workspace.root) / "uploads"

    def context(self):
        mode = "workspace" if self.workspace else "single"
        project = None
        if self.entry:
            project = {"id": self.entry["id"], "name": self.entry["name"]}
        return {"mode": mode, "open": self.repo is not None, "workspace_project": project,
                "data_dir": str(self.root) if self.root else None}

    def status(self):
        if self.repo is None:
            with self.job_lock:
                return {"project": None, **self.context(), "ffmpeg": media.tools_available(),
                        "busy": self.active is not None}
        repo = self.repo
        # A reloaded page can still show the last consistent snapshot and busy
        # state while FFmpeg or merging holds the repository mutation lock.
        if not repo.lock.acquire(blocking=False):
            with self.job_lock:
                result = deepcopy(self.snapshot)
                result["busy"] = self.active is not None
                return result
        try:
            result = repo.status()
            result.update(self.context())
            result["ffmpeg"] = media.tools_available()
            result["storage"] = self.storage
            result["preview_cache"] = self.preview_stats
            try:
                cloud = CloudProject.load(repo)
                result["cloud"] = {"connected": bool(cloud), "device": cloud.device if cloud else None,
                                   "folder": str(cloud.folder) if cloud else None}
            except UserError as error:
                result["cloud"] = {"connected": False, "error": str(error)}
            if result["project"]:
                ref = result["manifest"]
                result["preview_url"] = f"/media/preview/{ref}.mp4" if (self.root / "preview" / f"{ref}.mp4").exists() else None
            with self.job_lock:
                result["busy"] = self.active is not None
                self.snapshot = deepcopy(result)
            return result
        finally:
            repo.lock.release()

    def projects(self):
        if not self.workspace:
            raise UserError("Список проектов доступен при запуске без --data-dir.")
        return {"root": str(self.workspace.root), "projects": self.workspace.listing(),
                "current": self.entry["id"] if self.entry else None}

    def cloud_status(self):
        repo = self.require_repo()
        with repo.lock:
            cloud = CloudProject.load(repo)
            return cloud.status() if cloud else {"connected": False}

    def upload_path(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise UserError("Некорректный идентификатор загрузки.")
        path = self.upload_dir / token
        if not path.is_file():
            raise UserError("Загрузка не найдена. Выберите файл повторно.")
        return path

    def submit(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("action"), str):
            raise UserError("Укажите действие.")
        with self.job_lock:
            if self.active:
                raise UserError("Дождитесь завершения текущей операции.")
            job_id = uuid.uuid4().hex
            self.active = job_id
            self.jobs[job_id] = {"id": job_id, "status": "running", "progress": 0,
                                 "message": "Обработка…"}
            # Keep polling memory bounded in long-running local sessions.
            if len(self.jobs) > 200:
                for old in list(self.jobs)[:len(self.jobs) - 200]:
                    self.jobs.pop(old, None)
        self.executor.submit(self._run, job_id, data)
        return job_id

    def job(self, job_id):
        with self.job_lock:
            if job_id not in self.jobs:
                raise UserError("Операция не найдена.")
            return dict(self.jobs[job_id])

    def _update(self, job_id, **values):
        with self.job_lock:
            self.jobs[job_id].update(values)

    def ensure_preview(self, ref="working", progress=lambda value: None):
        manifest_id = self.repo.manifest_id(ref)
        path = self.root / "preview" / f"{manifest_id}.mp4"
        if not path.exists():
            media.render(self.repo.store, self.repo.manifest(ref), path, progress)
        else:
            os.utime(path)  # most recently used
        keep = {path.name, f"{self.repo.manifest_id()}.mp4"}
        self.preview_stats = trim_preview_cache(self.root / "preview", self.preview_cache, keep)
        return f"/media/preview/{manifest_id}.mp4"

    def _workspace_action(self, job_id, action, data):
        if not self.workspace:
            raise UserError("Управление проектами доступно при запуске без --data-dir.")
        progress = lambda p: self._update(job_id, progress=max(0, min(.99, p)))
        if action == "project_create":
            self.close_project()
            entry = self.workspace.create(data.get("name"))
            self.open_project(Path(entry["path"]), entry)
            return {"message": f"Проект «{entry['name']}» создан.", "project": entry["id"]}
        if action == "project_open":
            entry = self.workspace.entry(data.get("id"))
            if self.entry and self.entry["id"] == entry["id"]:
                return {"message": "Проект уже открыт.", "project": entry["id"]}
            if not Path(entry["path"]).is_dir():
                raise UserError("Папка проекта не найдена. Возможно, её переместили или удалили.")
            self.close_project()
            self.open_project(Path(entry["path"]), entry)
            return {"message": f"Открыт проект «{entry['name']}».", "project": entry["id"]}
        if action == "project_close":
            self.close_project()
            return {"message": "Проект закрыт."}
        if action == "project_forget":
            entry = self.workspace.entry(data.get("id"))
            if self.entry and self.entry["id"] == entry["id"]:
                raise UserError("Сначала закройте проект.")
            self.workspace.forget(entry["id"])
            return {"message": "Проект убран из списка. Его папка не удалена."}
        if action == "project_register":
            path = Path(str(data.get("path", "")).strip()).expanduser()
            if not path.is_absolute():
                raise UserError("Укажите полный путь к папке проекта.")
            entry = self.workspace.register(path)
            return {"message": f"Проект «{entry['name']}» добавлен в список.", "project": entry["id"]}
        if action == "project_migrate":
            entry = self.workspace.entry(data.get("id"))
            if self.entry and self.entry["id"] == entry["id"]:
                raise UserError("Закройте проект перед конвертацией.")
            original = data.get("source_media")
            original = Path(str(original).strip()).expanduser() if original else None
            if original is not None and not original.is_absolute():
                raise UserError("Укажите полный путь к исходному видеофайлу.")
            target = self.workspace.projects_dir / f"{entry['id']}-compact-{uuid.uuid4().hex[:6]}"
            report = maintenance.migrate(Path(entry["path"]), target, original,
                                         lambda text, value: self._update(job_id, progress=min(.99, value), message=text),
                                         store_factory=self.store_factory)
            copy = self.workspace.register(target, f"{entry['name']} (компактная копия)")
            self.workspace.update_storage(copy["id"], {"folder_bytes": report["after_bytes"]})
            return {"message": (f"Создана компактная копия: {maintenance.format_size(report['before_bytes'])} → "
                                f"{maintenance.format_size(report['after_bytes'])}. Исходный проект не изменён."),
                    "project": copy["id"], "report": report}
        if action == "project_clone_cloud":
            self.close_project()
            entry = self.workspace.create(data.get("name") or "Общий проект")
            try:
                self.open_project(Path(entry["path"]), entry)
                result = clone_from_cloud(self.repo, data.get("folder"), data.get("project"),
                                          data.get("source_device"), data.get("device"), progress)
            except Exception:
                self.close_project()
                self.workspace.forget(entry["id"])
                shutil.rmtree(entry["path"], ignore_errors=True)  # the folder was created by this action
                raise
            return {"message": "Общий проект получен. Дальше публикуйте и получайте версии на вкладке «Облако».",
                    "project": entry["id"], "branches": result["branches"]}
        raise UserError("Неизвестное действие.")

    def _run(self, job_id, data):
        source = None
        try:
            action = data["action"]
            if action in WORKSPACE_ACTIONS:
                result = self._workspace_action(job_id, action, data)
                self._update(job_id, status="done", progress=1, result=result)
                return
            repo = self.require_repo()
            with repo.lock:
                if action in {"import", "bundle_import"} or (action == "edit" and data.get("upload")):
                    source = self.upload_path(data.get("upload"))
                progress = lambda p: self._update(job_id, progress=min(.65, max(0, p) * .65))
                result = {"message": "Готово"}
                if action == "import":
                    if self.repo.state["project"]:
                        raise UserError("В этом проекте уже есть видео. Создайте новый проект в списке проектов.")
                    manifest = media.import_media(self.repo.store, source, progress)
                    result["commit"] = self.repo.initialise(manifest, data.get("name", "Видео"))
                    result["message"] = "Видео импортировано; первая версия сохранена."
                elif action == "edit":
                    self.repo.assert_no_merge()
                    edited = media.edit_media(self.repo.store, self.repo.manifest(), data.get("operation"),
                                              data.get("start"), data.get("end"), source,
                                              data.get("offset", 0), data.get("value"), progress)
                    self.repo.set_working(edited)
                    result["message"] = "Изменения применены к рабочей версии."
                elif action == "commit":
                    result["commit"] = self.repo.commit(data.get("message", ""), data.get("author", "Local editor"))
                    result["message"] = "Версия сохранена."
                elif action == "branch":
                    self.repo.branch(data.get("name", ""))
                    result["message"] = "Ветка создана."
                elif action == "checkout":
                    self.repo.checkout(data.get("name"))
                    result["message"] = "Ветка переключена. Черновик предыдущей ветки сохранён."
                elif action == "restore":
                    self.repo.restore(data.get("ref"))
                    result["message"] = "Версия восстановлена как черновик; история сохранена."
                elif action == "discard":
                    self.repo.discard()
                    result["message"] = "Черновик сброшен к сохранённой версии."
                elif action == "merge":
                    result.update(self.repo.merge(data.get("source")))
                elif action == "resolve":
                    self.repo.resolve(data.get("conflict"), data.get("choice"))
                    result["message"] = "Вариант выбран. После разрешения всех конфликтов сохраните коммит."
                elif action == "cancel_merge":
                    self.repo.cancel_merge()
                    result["message"] = "Слияние отменено."
                elif action in {"preview", "export"}:
                    url = self.ensure_preview(data.get("ref", "working"), progress)
                    result["preview_url"] = url
                    if action == "export":
                        result["download_url"] = url + "?download=1"
                        result["message"] = "Видео готово к скачиванию."
                elif action == "bundle_export":
                    for old in (self.root / "bundles").glob("*.vmrepo"):
                        old.unlink(missing_ok=True)  # earlier exports were already offered for download
                    filename = uuid.uuid4().hex + ".vmrepo"
                    self.repo.export_bundle(self.root / "bundles" / filename, lambda p: self._update(job_id, progress=p * .99))
                    result["download_url"] = f"/media/bundles/{filename}"
                    result["message"] = "Репозиторий готов к передаче на другой компьютер."
                elif action == "bundle_import":
                    opening_project = self.repo.state["project"] is None
                    self.repo.import_bundle(source, data.get("peer", "peer"), progress)
                    result["message"] = (
                        "Репозиторий открыт. История и ветки восстановлены." if opening_project else
                        "Репозиторий получен. Входящие ветки доступны для слияния.")
                elif action == "cloud_connect":
                    cloud = CloudProject.connect(self.repo, data.get("folder"), data.get("device"))
                    result["message"] = f"Общая папка подключена. Это устройство: {cloud.device}."
                elif action == "cloud_disconnect":
                    CloudProject.disconnect(self.repo)
                    result["message"] = "Общая папка отключена. Файлы в ней не удалены."
                elif action == "cloud_publish":
                    cloud = self._cloud()
                    published = cloud.publish(lambda p: self._update(job_id, progress=p * .99))
                    result.update(published)
                    result["message"] = (f"Опубликованы ветки: {', '.join(published['branches'])}. Скопировано файлов: "
                                         f"{published['copied']} ({maintenance.format_size(published['copied_bytes'])}), "
                                         f"уже были в папке: {published['skipped']}.")
                    if self.repo.dirty():
                        result["message"] += " Несохранённый черновик не публикуется."
                elif action == "cloud_receive":
                    received = self._cloud().receive(data.get("device"), progress)
                    result.update(received)
                    result["message"] = (f"Получены ветки: {', '.join(received['branches'])}. "
                                         "Влейте нужную на вкладке «Слияние».")
                else:
                    raise UserError("Неизвестное действие.")
                if self.repo.state["project"] and action not in {"export", "preview", "bundle_export", "cloud_connect",
                                                                  "cloud_disconnect", "cloud_publish"}:
                    self._update(job_id, progress=.67, message="Подготовка предпросмотра…")
                    try:
                        result["preview_url"] = self.ensure_preview(
                            progress=lambda p: self._update(job_id, progress=.67 + .32 * p))
                    except (UserError, OSError) as error:
                        # Editing and committing remain valid even if an optional MP4 preview fails.
                        result["message"] += " Предпросмотр недоступен: " + str(error)
                self.storage = store_stats(self.repo)
                self._update(job_id, status="done", progress=1, result=result)
        except UserError as error:
            self._update(job_id, status="error", error=str(error))
        except Exception:
            traceback.print_exc()
            self._update(job_id, status="error", error="Внутренняя ошибка. Подробности в терминале приложения.")
        finally:
            if source:
                source.unlink(missing_ok=True)
            with self.job_lock:
                self.active = None

    def _cloud(self):
        cloud = CloudProject.load(self.repo)
        if not cloud:
            raise UserError("Сначала подключите общую папку облачного клиента.")
        return cloud


class Handler(BaseHTTPRequestHandler):
    server_version = "Vibemogging/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def _local_request(self, mutation=False):
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in allowed:
            self.json_response({"error": "Разрешено только локальное подключение."}, 403)
            return False
        origin = self.headers.get("Origin")
        if mutation and origin and origin != f"http://{host}":
            self.json_response({"error": "Запрос с другого сайта отклонён."}, 403)
            return False
        return True

    def send_bytes(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json_response(self, value, status=200):
        self.send_bytes(canonical(value), "application/json; charset=utf-8", status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._local_request():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            if path == "/api/status":
                self.json_response(self.app.status())
            elif path.startswith("/api/jobs/"):
                self.json_response(self.app.job(path.rsplit("/", 1)[-1]))
            elif path == "/api/projects":
                self.json_response(self.app.projects())
            elif path == "/api/cloud":
                self.json_response(self.app.cloud_status())
            elif path == "/api/cloud/projects":
                self.json_response({"projects": list_shared_projects(query.get("folder", ""))})
            elif path == "/api/diff":
                repo = self.app.require_repo()
                with repo.lock:
                    self.json_response(repo.diff(query.get("left", repo.head), query.get("right", "working")))
            elif path == "/api/frame":
                repo = self.app.require_repo()
                with repo.lock:
                    manifest = repo.manifest(query.get("ref", "working"))
                    other = repo.manifest(query["diff_with"]) if query.get("diff_with") else None
                    data = media.png_frame(repo.store, manifest, int(query.get("frame", 0)), other)
                    self.send_bytes(data, "image/png")
            elif path == "/api/audio":
                repo = self.app.require_repo()
                with repo.lock:
                    manifest = repo.manifest(query.get("ref", "working"))
                    start, end = media.interval(manifest, query.get("start", 0),
                                                query.get("end", min(60, manifest["samples"] / SAMPLE_RATE)))
                    if end - start > 60 * SAMPLE_RATE:
                        raise UserError("Для сравнения выбирайте не более 60 секунд звука.")
                    buffer = io.BytesIO()
                    with wave.open(buffer, "wb") as audio:
                        audio.setnchannels(CHANNELS)
                        audio.setsampwidth(2)
                        audio.setframerate(SAMPLE_RATE)
                        audio.writeframes(read_audio(repo.store, manifest, start * FRAME_BYTES, end * FRAME_BYTES))
                    self.send_bytes(buffer.getvalue(), "audio/wav")
            elif path.startswith("/media/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise UserError("Файл не найден.")
                _, kind, name = parts
                self.app.require_repo()
                if kind == "preview" and name.endswith(".mp4") and valid_id(name[:-4]):
                    target = self.app.root / kind / name
                    if target.is_file():
                        try:
                            os.utime(target)  # keeps a watched preview out of LRU eviction
                        except OSError:
                            pass
                    self.send_file(target, "video/mp4", bool(query.get("download")))
                elif kind == "bundles" and re.fullmatch(r"[a-f0-9]{32}\.vmrepo", name):
                    self.send_file(self.app.root / kind / name, "application/zip", True)
                else:
                    raise UserError("Файл не найден.")
            else:
                relative = "index.html" if path == "/" else path.lstrip("/")
                target = (STATIC / relative).resolve()
                if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                    if path == "/" and not (STATIC / "index.html").exists():
                        self.send_bytes(b"<!doctype html><meta charset=utf-8><title>Vibemogging</title><h1>Vibemogging backend is running</h1><p>The UI is being developed on feature/v1-ui.</p><a href='/api/status'>API status</a>", "text/html; charset=utf-8")
                    else:
                        self.json_response({"error": "Страница не найдена."}, 404)
                else:
                    mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
                    self.send_bytes(target.read_bytes(), mime + ("; charset=utf-8" if mime.startswith("text/") or mime.endswith("javascript") else ""))
        except (UserError, ValueError) as error:
            self.json_response({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.json_response({"error": "Внутренняя ошибка сервера."}, 500)

    def do_POST(self):
        if not self._local_request(mutation=True):
            self.close_connection = True
            return
        try:
            if self.app.busy():
                self.close_connection = True
                self.json_response({"error": "Дождитесь завершения текущей операции."}, 409)
                return
            length = int(self.headers.get("Content-Length", "-1"))
            path = urlsplit(self.path).path
            if path == "/api/upload":
                if self.headers.get_content_type() != "application/octet-stream":
                    raise UserError("Отправьте файл как application/octet-stream.")
                if not 0 < length <= MAX_UPLOAD:
                    raise UserError("Размер загрузки должен быть от 1 байта до 4 ГиБ.")
                token = uuid.uuid4().hex
                target = self.app.upload_dir / token
                self.connection.settimeout(120)
                try:
                    with target.open("xb") as output:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise UserError("Загрузка прервана.")
                            output.write(chunk)
                            remaining -= len(chunk)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                finally:
                    self.connection.settimeout(None)
                self.json_response({"upload": token})
            elif path == "/api/action":
                if self.headers.get_content_type() != "application/json" or not 0 < length <= 1024 * 1024:
                    raise UserError("Ожидается JSON размером до 1 МиБ.")
                data = json.loads(self.rfile.read(length))
                self.json_response({"job": self.app.submit(data)}, 202)
            else:
                self.close_connection = True
                self.json_response({"error": "Действие не найдено."}, 404)
        except (UserError, ValueError, TimeoutError) as error:
            self.close_connection = True
            self.json_response({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.close_connection = True
            self.json_response({"error": "Внутренняя ошибка сервера."}, 500)

    def send_file(self, path, content_type, attachment=False):
        if not path.is_file():
            self.json_response({"error": "Файл не найден. Подготовьте предпросмотр или экспорт."}, 404)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                self._range_error(size)
                return
            left, right = match.groups()
            if left:
                start, end = int(left), min(int(right), end) if right else end
            else:
                start = max(0, size - int(right))
            if not 0 <= start <= end < size:
                self._range_error(size)
                return
        self.send_response(206 if requested else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if attachment:
            filename = "vibemogging.vmrepo" if content_type == "application/zip" else "vibemogging.mp4"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                data = stream.read(min(1024 * 1024, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def _range_error(self, size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        if hasattr(self, "app"):
            self.app.close()
        super().server_close()


def make_server(root: Path | None = None, port=8765, workspace: Path | None = None, store_factory=Store,
                preview_cache=DEFAULT_PREVIEW_CACHE):
    """`root` opens one project folder (legacy --data-dir); `workspace` shows the project list."""
    server = LocalServer(("127.0.0.1", port), Handler)
    try:
        server.app = App(root, Workspace(workspace) if workspace else None, store_factory, preview_cache)
    except Exception:
        server.server_close()
        raise
    return server
