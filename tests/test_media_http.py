import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from vibemogging import media
from vibemogging.merge import FRAME_BYTES, SAMPLE_RATE, read_audio
from vibemogging.server import make_server
from vibemogging.locking import DirectoryLock
from vibemogging.storage import Store, UserError


@unittest.skipUnless(media.tools_available(), "FFmpeg and ffprobe are required")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.source = cls.root / "source.mp4"
        subprocess.run(media.command("-f", "lavfi", "-i", "color=c=red:s=32x24:r=5/2:d=4",
                                     "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
                                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.source),
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_import_edit_export_fractional_fps(self):
        store = Store(self.root / "store")
        manifest = media.import_media(store, self.source)
        self.assertEqual(manifest["fps"], [5, 2])
        self.assertEqual(len(manifest["video"]), 10)
        self.assertEqual(manifest["samples"], 4 * SAMPLE_RATE)
        edited = media.edit_media(store, manifest, "fill", .8, 2, value="#123456")
        edited = media.edit_media(store, edited, "mute", 1, 2)
        self.assertEqual(edited["video"][:2], manifest["video"][:2])
        self.assertEqual(edited["video"][5:], manifest["video"][5:])
        self.assertEqual(store.get("video", edited["video"][2]), bytes.fromhex("123456") * (32 * 24))
        self.assertEqual(read_audio(store, edited, SAMPLE_RATE * FRAME_BYTES, 2 * SAMPLE_RATE * FRAME_BYTES), b"\0" * (SAMPLE_RATE * FRAME_BYTES))
        output = self.root / "result.mp4"
        media.render(store, edited, output)
        metadata = media.probe(output)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        self.assertEqual(int(video["nb_frames"]), 10)
        self.assertEqual(video["avg_frame_rate"], "5/2")
        self.assertAlmostEqual(float(video["duration"]), 4, places=2)
        self.assertTrue(media.png_frame(store, manifest, 0).startswith(b"\x89PNG"))

    def test_replacements_and_short_source(self):
        store = Store(self.root / "replacements")
        manifest = media.import_media(store, self.source)
        replaced = media.edit_media(store, manifest, "replace_video", 0, 2, self.source, offset=1)
        self.assertEqual(len(replaced["video"]), len(manifest["video"]))
        replaced = media.edit_media(store, replaced, "replace_audio", 0, 1, self.source, offset=1)
        self.assertEqual(replaced["samples"], manifest["samples"])
        with self.assertRaises(UserError):
            media.edit_media(store, manifest, "replace_video", 0, 4, self.source, offset=3)
        with self.assertRaises(UserError):
            media.edit_media(store, manifest, "replace_audio", 0, 4, self.source, offset=3)

    def test_silent_video_gets_exact_silence_track(self):
        source = self.root / "silent.mp4"
        subprocess.run(media.command("-i", self.source, "-c:v", "copy", "-an", source), check=True, capture_output=True)
        store = Store(self.root / "silent-store")
        manifest = media.import_media(store, source)
        self.assertEqual(read_audio(store, manifest, 0, 1000), b"\0" * 1000)

    def test_audio_start_offset_is_preserved(self):
        source = self.root / "delayed.mkv"
        subprocess.run(media.command("-f", "lavfi", "-i", "color=c=black:s=16x16:r=2:d=2",
                                     "-itsoffset", "1", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                                     "-c:v", "ffv1", "-c:a", "pcm_s16le", "-t", "2", source),
                       check=True, capture_output=True)
        store = Store(self.root / "delayed-store")
        manifest = media.import_media(store, source)
        self.assertEqual(read_audio(store, manifest, 0, SAMPLE_RATE * FRAME_BYTES), b"\0" * (SAMPLE_RATE * FRAME_BYTES))
        self.assertNotEqual(read_audio(store, manifest, SAMPLE_RATE * FRAME_BYTES, 2 * SAMPLE_RATE * FRAME_BYTES), b"\0" * (SAMPLE_RATE * FRAME_BYTES))

    def test_data_directory_lock(self):
        path = self.root / "locked"
        first = DirectoryLock(path)
        try:
            with self.assertRaises(UserError):
                DirectoryLock(path)
        finally:
            first.close()
        second = DirectoryLock(path)
        second.close()

    def test_video_start_offset_keeps_audio_timeline(self):
        source = self.root / "delayed-video.mkv"
        subprocess.run(media.command("-itsoffset", "1", "-f", "lavfi", "-i", "color=c=blue:s=16x16:r=2:d=1",
                                     "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                                     "-c:v", "ffv1", "-c:a", "pcm_s16le", "-t", "2", source),
                       check=True, capture_output=True)
        store = Store(self.root / "delayed-video-store")
        manifest = media.import_media(store, source)
        self.assertEqual(len(manifest["video"]), 4)
        self.assertEqual(manifest["samples"], 2 * SAMPLE_RATE)
        self.assertTrue(any(read_audio(store, manifest, 0, 1000)), "Early soundtrack was lost")

    def test_real_http_upload_job_edit_export_and_ranges(self):
        server = make_server(self.root / "http-repo", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = (response.status, dict(response.getheaders()), response.read())
            connection.close()
            return result

        def action(data):
            status, _, raw = request("POST", "/api/action", json.dumps(data), {"Content-Type": "application/json"})
            self.assertEqual(status, 202, raw)
            job_id = json.loads(raw)["job"]
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                _, _, raw = request("GET", "/api/jobs/" + job_id)
                job = json.loads(raw)
                if job["status"] == "error":
                    self.fail(job["error"])
                if job["status"] == "done":
                    return job["result"]
                time.sleep(.05)
            self.fail("Job timed out")

        try:
            status, _, raw = request("GET", "/api/status")
            self.assertEqual(status, 200)
            self.assertIsNone(json.loads(raw)["project"])
            status, _, raw = request("POST", "/api/upload?name=source.mp4", self.source.read_bytes(), {"Content-Type": "application/octet-stream"})
            self.assertEqual(status, 200)
            token = json.loads(raw)["upload"]
            action({"action": "import", "upload": token, "name": "source.mp4"})
            action({"action": "edit", "operation": "fill", "start": 0, "end": 1, "value": "#0099ff"})
            action({"action": "commit", "message": "blue start"})
            result = action({"action": "export"})
            status, headers, content = request("GET", result["download_url"], headers={"Range": "bytes=0-99"})
            self.assertEqual(status, 206)
            self.assertEqual(len(content), 100)
            self.assertIn("attachment", headers["Content-Disposition"])
            status, _, _ = request("GET", result["download_url"], headers={"Range": "bytes=999999999-"})
            self.assertEqual(status, 416)
            status, _, raw = request("GET", "/api/status")
            state = json.loads(raw)
            self.assertFalse(state["dirty"])
            self.assertEqual(len(state["history"]), 2)
            status, _, png = request("GET", "/api/frame?frame=0")
            self.assertEqual(status, 200)
            self.assertTrue(png.startswith(b"\x89PNG"))
            status, _, wav = request("GET", "/api/audio?start=0&end=1")
            self.assertEqual(status, 200)
            self.assertTrue(wav.startswith(b"RIFF"))
            action({"action": "branch", "name": "teammate"})
            action({"action": "edit", "operation": "fill", "start": 0, "end": 1, "value": "#ffee00"})
            action({"action": "commit", "message": "incoming yellow"})
            action({"action": "checkout", "name": "main"})
            action({"action": "edit", "operation": "fill", "start": 0, "end": 1, "value": "#008800"})
            action({"action": "commit", "message": "local green"})
            self.assertEqual(action({"action": "merge", "source": "teammate"})["mode"], "conflicts")
            _, _, raw = request("GET", "/api/status")
            conflicts = json.loads(raw)["merge"]["conflicts"]
            self.assertEqual(len(conflicts), 1)
            action({"action": "resolve", "conflict": conflicts[0]["id"], "choice": "theirs"})
            action({"action": "commit", "message": "resolved yellow"})
            _, _, raw = request("GET", "/api/diff?left=teammate&right=main")
            self.assertEqual(json.loads(raw)["video_frames"], 0)
            result = action({"action": "bundle_export"})
            status, headers, bundle = request("GET", result["download_url"])
            self.assertEqual(status, 200)
            self.assertIn("attachment", headers["Content-Disposition"])
            self.assertTrue(bundle.startswith(b"PK"))
            status, _, _ = request("POST", "/api/action", '{"action":"discard"}',
                                    {"Content-Type": "application/json", "Origin": "https://unrelated.example"})
            self.assertEqual(status, 403)
            status, _, _ = request("GET", "/api/status", headers={"Host": "unrelated.example"})
            self.assertEqual(status, 403)
            status, _, _ = request("GET", "/../../etc/passwd")
            self.assertEqual(status, 404)
        finally:
            server.shutdown()
            server.app.executor.shutdown(wait=True)
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
