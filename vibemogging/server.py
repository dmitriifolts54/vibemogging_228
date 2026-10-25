"""Loopback-only HTTP application and serialized background media jobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
from pathlib import Path
import re
import threading
import traceback
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import wave

from . import media
from .locking import DirectoryLock
from .merge import CHANNELS, FRAME_BYTES, SAMPLE_RATE, read_audio
from .repository import Repository
from .storage import UserError, canonical, valid_id

MAX_UPLOAD = 4 * 1024 ** 3
STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self, root: Path):
        self.directory_lock = DirectoryLock(Path(root))
        try:
            self.repo = Repository(root)
            self.root = self.repo.root
            for directory in ("uploads", "preview", "bundles"):
                (self.root / directory).mkdir(exist_ok=True)
        except Exception:
            self.directory_lock.close()
            raise
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media")
        self.jobs = {}
        self.job_lock = threading.Lock()
        self.active = None
        self.snapshot = {"project": None, "ffmpeg": media.tools_available(), "busy": False}
        self.status()

    def close(self):
        self.executor.shutdown(wait=True)
        self.directory_lock.close()

    def busy(self):
        with self.job_lock:
            return self.active is not None

    def status(self):
        # A reloaded page can still show the last consistent snapshot and busy
        # state while FFmpeg or merging holds the repository mutation lock.
        if not self.repo.lock.acquire(blocking=False):
            with self.job_lock:
                result = deepcopy(self.snapshot)
                result["busy"] = self.active is not None
                return result
        try:
            result = self.repo.status()
            result["ffmpeg"] = media.tools_available()
            if result["project"]:
                ref = result["manifest"]
                result["preview_url"] = f"/media/preview/{ref}.mp4" if (self.root / "preview" / f"{ref}.mp4").exists() else None
            with self.job_lock:
                result["busy"] = self.active is not None
                self.snapshot = deepcopy(result)
            return result
        finally:
            self.repo.lock.release()

    def upload_path(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise UserError("Некорректный идентификатор загрузки.")
        path = self.root / "uploads" / token
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
        return f"/media/preview/{manifest_id}.mp4"

    def _run(self, job_id, data):
        source = None
        try:
            with self.repo.lock:
                action = data["action"]
                if action in {"import", "bundle_import"} or (action == "edit" and data.get("upload")):
                    source = self.upload_path(data.get("upload"))
                progress = lambda p: self._update(job_id, progress=min(.65, max(0, p) * .65))
                result = {"message": "Готово"}
                if action == "import":
                    if self.repo.state["project"]:
                        raise UserError("Проект уже открыт. Для нового запустите приложение с другим --data-dir.")
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
                    filename = uuid.uuid4().hex + ".vmrepo"
                    self.repo.export_bundle(self.root / "bundles" / filename)
                    result["download_url"] = f"/media/bundles/{filename}"
                    result["message"] = "Репозиторий готов к передаче на другой компьютер."
                elif action == "bundle_import":
                    self.repo.import_bundle(source, data.get("peer", "peer"))
                    result["message"] = "Репозиторий получен. Входящие ветки доступны для слияния."
                else:
                    raise UserError("Неизвестное действие.")
                if self.repo.state["project"] and action not in {"export", "preview", "bundle_export"}:
                    self._update(job_id, progress=.67, message="Подготовка предпросмотра…")
                    try:
                        result["preview_url"] = self.ensure_preview(
                            progress=lambda p: self._update(job_id, progress=.67 + .32 * p))
                    except (UserError, OSError) as error:
                        # Editing and committing remain valid even if an optional MP4 preview fails.
                        result["message"] += " Предпросмотр недоступен: " + str(error)
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
            elif path == "/api/diff":
                with self.app.repo.lock:
                    self.json_response(self.app.repo.diff(query.get("left", self.app.repo.head), query.get("right", "working")))
            elif path == "/api/frame":
                with self.app.repo.lock:
                    manifest = self.app.repo.manifest(query.get("ref", "working"))
                    other = self.app.repo.manifest(query["diff_with"]) if query.get("diff_with") else None
                    data = media.png_frame(self.app.repo.store, manifest, int(query.get("frame", 0)), other)
                    self.send_bytes(data, "image/png")
            elif path == "/api/audio":
                with self.app.repo.lock:
                    manifest = self.app.repo.manifest(query.get("ref", "working"))
                    start, end = media.interval(manifest, query.get("start", 0),
                                                query.get("end", min(60, manifest["samples"] / SAMPLE_RATE)))
                    if end - start > 60 * SAMPLE_RATE:
                        raise UserError("Для сравнения выбирайте не более 60 секунд звука.")
                    buffer = io.BytesIO()
                    with wave.open(buffer, "wb") as audio:
                        audio.setnchannels(CHANNELS)
                        audio.setsampwidth(2)
                        audio.setframerate(SAMPLE_RATE)
                        audio.writeframes(read_audio(self.app.repo.store, manifest, start * FRAME_BYTES, end * FRAME_BYTES))
                    self.send_bytes(buffer.getvalue(), "audio/wav")
            elif path.startswith("/media/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise UserError("Файл не найден.")
                _, kind, name = parts
                if kind == "preview" and name.endswith(".mp4") and valid_id(name[:-4]):
                    self.send_file(self.app.root / kind / name, "video/mp4", bool(query.get("download")))
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
                target = self.app.root / "uploads" / token
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


def make_server(root: Path, port=8765):
    server = LocalServer(("127.0.0.1", port), Handler)
    try:
        server.app = App(root)
    except Exception:
        server.server_close()
        raise
    return server
