import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from vibemogging import media
from vibemogging.server import make_server


@unittest.skipUnless(media.tools_available(), "FFmpeg and ffprobe are required")
class WorkspaceHttpTests(unittest.TestCase):
    """Project chooser and cloud folder through the real HTTP server."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "clip.mp4"
        subprocess.run(media.command("-f", "lavfi", "-i", "color=c=red:s=32x24:r=5:d=2",
                                     "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", self.source),
                       check=True, capture_output=True)
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def start(self, workspace):
        server = make_server(port=0, workspace=workspace)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data) if data[:1] in (b"{", b"[") else data

    def action(self, port, **payload):
        status, body = self.request(port, "POST", "/api/action", json.dumps(payload).encode(),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 202, body)
        while True:
            _, job = self.request(port, "GET", f"/api/jobs/{body['job']}")
            if job["status"] != "running":
                return job
            time.sleep(.1)

    def ok(self, port, **payload):
        job = self.action(port, **payload)
        self.assertEqual(job["status"], "done", job)
        return job["result"]

    def upload(self, port, path):
        status, body = self.request(port, "POST", f"/api/upload?name={path.name}", path.read_bytes(),
                                    {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, body)
        return body["upload"]

    def test_chooser_create_import_close_reopen_and_cloud_round_trip(self):
        port = self.start(self.root / "ws-a")
        _, status = self.request(port, "GET", "/api/status")
        self.assertEqual((status["mode"], status["open"]), ("workspace", False))
        status_code, _ = self.request(port, "GET", "/api/frame?frame=0")
        self.assertEqual(status_code, 400, "media routes need an open project")

        created = self.ok(port, action="project_create", name="Отпуск")
        self.ok(port, action="import", upload=self.upload(port, self.source), name="clip.mp4")
        _, status = self.request(port, "GET", "/api/status")
        self.assertTrue(status["open"])
        self.assertEqual(status["workspace_project"]["name"], "Отпуск")
        self.assertIn("folder_bytes", status["storage"])
        self.ok(port, action="project_close")
        _, listing = self.request(port, "GET", "/api/projects")
        self.assertEqual([p["name"] for p in listing["projects"]], ["Отпуск"])
        self.assertTrue(listing["projects"][0]["initialised"])
        self.ok(port, action="project_open", id=created["project"])

        shared = self.root / "cloud"
        shared.mkdir()
        error = self.action(port, action="cloud_connect", folder="relative", device="pc1")
        self.assertEqual(error["status"], "error")
        self.ok(port, action="cloud_connect", folder=str(shared), device="pc1")
        published = self.ok(port, action="cloud_publish")
        self.assertGreater(published["copied"], 0)
        _, projects = self.request(port, "GET", f"/api/cloud/projects?folder={shared}")
        project_id = projects["projects"][0]["id"]

        other = self.start(self.root / "ws-b")
        cloned = self.ok(other, action="project_clone_cloud", folder=str(shared), project=project_id,
                         source_device="pc1", device="pc2", name="Отпуск (ПК2)")
        self.assertEqual(cloned["branches"], ["main"])
        self.ok(other, action="edit", operation="fill", start=0, end=1, value="#0000ff")
        self.ok(other, action="commit", message="ПК2: синий")
        self.ok(other, action="cloud_publish")

        _, cloud = self.request(port, "GET", "/api/cloud")
        pc2 = next(d for d in cloud["devices"] if d["device"] == "pc2")
        self.assertTrue(pc2["latest"]["complete"])
        received = self.ok(port, action="cloud_receive", device="pc2")
        self.assertEqual(received["branches"], ["pc2/main"])
        merged = self.ok(port, action="merge", source="pc2/main")
        self.assertEqual(merged["mode"], "fast-forward")

    def test_preview_cache_is_bounded(self):
        server = make_server(self.root / "cached", port=0, preview_cache=1)
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        self.ok(port, action="import", upload=self.upload(port, self.source), name="clip.mp4")
        for second in (0, 1):
            self.ok(port, action="edit", operation="fill", start=second, end=second + 1, value="#00ff00")
        export = self.ok(port, action="export")
        previews = list((self.root / "cached" / "preview").glob("*.mp4"))
        _, status = self.request(port, "GET", "/api/status")
        self.assertEqual([p.name for p in previews], [f"{status['manifest']}.mp4"],
                         "older previews are evicted; the current one stays")
        self.assertEqual(status["preview_cache"]["preview_files"], 1)
        code, _ = self.request(port, "GET", export["download_url"])
        self.assertEqual(code, 200)

    def test_single_directory_mode_has_no_chooser(self):
        server = make_server(self.root / "single", port=0)
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        _, status = self.request(port, "GET", "/api/status")
        self.assertEqual((status["mode"], status["open"]), ("single", True))
        code, _ = self.request(port, "GET", "/api/projects")
        self.assertEqual(code, 400)
        job = self.action(port, action="project_create", name="x")
        self.assertEqual(job["status"], "error")


if __name__ == "__main__":
    unittest.main()
