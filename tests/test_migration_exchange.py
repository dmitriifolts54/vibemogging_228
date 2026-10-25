import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vibemogging import maintenance, media
from vibemogging.cloud import CloudProject
from vibemogging.repository import Repository
from vibemogging.storage import UserError
from tests.compact_fake import compact_store_class


def tree_digest(root: Path):
    """Content of every file, so tests can prove the source folder was not changed."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "process.lock":
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@unittest.skipUnless(media.tools_available(), "FFmpeg and ffprobe are required")
class MigrationTests(unittest.TestCase):
    """Legacy RGB repository → compact copy, then exchange of assets and recipes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        source = self.root / "source.mp4"
        subprocess.run(media.command("-f", "lavfi", "-i", "testsrc2=s=64x36:r=10:d=3",
                                     "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source),
                       check=True, capture_output=True)
        self.legacy = Repository(self.root / "legacy")
        # compact=False reproduces the V1 layout: one zlib RGB object per frame.
        manifest = media.import_media(self.legacy.store, source, compact=False)
        self.legacy.initialise(manifest, "source.mp4")
        # History with a branch, an uncommitted draft and a pending video conflict.
        self.legacy.branch("b")
        b = media.edit_media(self.legacy.store, self.legacy.manifest(), "brighten", 1, 2)
        self.legacy.set_working(b)
        self.legacy.commit("B brighten 1–2")
        self.legacy.checkout("main")
        a = media.edit_media(self.legacy.store, self.legacy.manifest(), "darken", 1.5, 2.5)
        self.legacy.set_working(a)
        self.legacy.commit("A darken 1.5–2.5")
        self.assertEqual(self.legacy.merge("b")["mode"], "conflicts")
        self.legacy.resolve(self.legacy.state["merge"]["conflicts"][0]["id"], "theirs")
        self.state_before = json.loads((self.legacy.root / "state.json").read_text())
        self.files_before = tree_digest(self.legacy.root)
        self.Store = compact_store_class()

    def open_compact(self, path):
        return Repository(path, store=self.Store(path))

    def migrate(self, target, **kwargs):
        return maintenance.migrate(self.legacy.root, target, store_factory=self.Store, clip_frames=8, **kwargs)

    def test_migration_preserves_history_drafts_conflicts_and_raw_ids(self):
        target = self.root / "compact"
        report = self.migrate(target)
        self.assertEqual(tree_digest(self.legacy.root), self.files_before, "source must stay untouched")
        self.assertEqual(json.loads((target / "state.json").read_text()), self.state_before)
        self.assertEqual(report["fallback"], 0)
        self.assertGreater(report["kept_recipes"], 0, "brighten/darken LUT recipes are carried over")
        self.assertEqual(report["packed"] + report["kept_recipes"], report["frames"])
        self.assertEqual(report["verified_frames"], report["frames"])
        self.assertEqual(len(list((target / "objects").rglob("*"))) and
                         sum(1 for p in (target / "objects").rglob("*") if p.is_file()),
                         report["commits"] + report["manifests"] + report["audio_blocks"] + 2,
                         "no RGB frame objects remain: only metadata, audio and two LUT tables")
        self.assertGreater(report["before_bytes"], 0)
        compact = self.open_compact(target)
        for manifest_id in maintenance.manifests_to_keep(self.legacy)[1]:
            manifest = self.legacy.store.get_json("manifest", manifest_id)
            for ref in manifest["video"]:
                self.assertEqual(compact.store.get("video", ref), self.legacy.store.get("video", ref))
        self.assertEqual(compact.status()["merge"]["conflicts"][0]["choice"], "theirs")
        compact.commit("Finish merge after migration")
        self.assertEqual(len(compact.get_commit(compact.head)["parents"]), 2)
        self.assertFalse(list(self.root.glob(".compact.partial-*")))
        print(f"\n[замер] legacy {report['before_bytes']} Б → compact {report['after_bytes']} Б, "
              f"кадров {report['frames']}, клипов {report['clips']}")

    def test_original_source_and_v1_brightness_become_recipes(self):
        legacy = Repository(self.root / "legacy-v1")
        legacy.initialise(media.import_media(legacy.store, self.root / "source.mp4", compact=False), "source.mp4")
        # V1 stored brighten/darken results as plain RGB objects (no LUT recipe).
        manifest = legacy.manifest()
        for index in range(5, 10):
            table = maintenance.LUT_TABLES[index % 2]
            manifest["video"][index] = legacy.store.put("video", legacy.store.get("video", manifest["video"][index]).translate(table))
        legacy.set_working(manifest)
        legacy.commit("V1 brightness")
        target = self.root / "from-source"
        report = maintenance.migrate(legacy.root, target, self.root / "source.mp4", store_factory=self.Store)
        self.assertTrue(report["original_matched"])
        self.assertEqual(report["recovered_luts"], 5)
        self.assertEqual(report["packed"], 0)
        self.assertEqual(report["verified_frames"], report["frames"])
        compact = self.open_compact(target)
        for ref in compact.manifest()["video"]:
            self.assertEqual(compact.store.get("video", ref), legacy.store.get("video", ref))

    def test_existing_target_and_failures_leave_everything_untouched(self):
        target = self.root / "exists"
        target.mkdir()
        with self.assertRaises(UserError):
            self.migrate(target)
        failing = self.root / "failing"
        with patch.object(maintenance, "encode_clip", side_effect=UserError("boom")):
            with self.assertRaises(UserError):
                self.migrate(failing)
        self.assertFalse(failing.exists())
        self.assertFalse(list(self.root.glob(".failing.partial-*")))
        self.assertEqual(tree_digest(self.legacy.root), self.files_before)

    def test_frames_that_do_not_round_trip_stay_lossless_objects(self):
        target = self.root / "fallback"
        with patch.object(maintenance, "decoded_ids", return_value=[]):
            report = self.migrate(target)
        self.assertEqual(report["packed"], 0)
        self.assertEqual(report["fallback"] + report["kept_recipes"], report["frames"])
        compact = self.open_compact(target)
        self.assertEqual(compact.manifest(), self.legacy.manifest())

    def test_compact_bundle_and_cloud_exchange_carry_assets_and_recipes(self):
        target = self.root / "compact"
        self.migrate(target)
        compact = self.open_compact(target)
        compact.cancel_merge()
        archive = self.root / "compact.vmrepo"
        compact.export_bundle(archive)
        with zipfile.ZipFile(archive) as bundle:
            assets = [n for n in bundle.namelist() if n.startswith("assets/")]
            self.assertTrue(assets)
            self.assertEqual(bundle.getinfo(assets[0]).compress_type, zipfile.ZIP_STORED)
            self.assertFalse(any(n.startswith("objects/video/") for n in bundle.namelist()
                                 if n.split("/")[-1] in json.loads(bundle.read("recipes.json"))))
        clone = self.open_compact(self.root / "clone")
        clone.import_bundle(archive)
        self.assertEqual(clone.manifest(), compact.manifest())
        for ref in clone.manifest()["video"]:
            self.assertEqual(clone.store.get("video", ref), compact.store.get("video", ref))

        tampered = self.root / "tampered.vmrepo"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as out:
            for name in source.namelist():
                data = source.read(name)
                if name == assets[0]:
                    data = data[:-1] + bytes([data[-1] ^ 1])
                out.writestr(name, data)
        victim = self.open_compact(self.root / "victim")
        with self.assertRaises(UserError):
            victim.import_bundle(tampered)
        self.assertIsNone(victim.state["project"])

        shared = self.root / "cloud"
        shared.mkdir()
        CloudProject.connect(compact, str(shared), "pc1").publish()
        from vibemogging.cloud import clone_from_cloud
        second = self.open_compact(self.root / "second")
        clone_from_cloud(second, str(shared), compact.state["project"]["id"], "pc1", "pc2")
        self.assertEqual(second.manifest(), compact.manifest())
        republish = CloudProject.load(compact).publish()
        self.assertEqual(republish["copied"], 0, "assets are never copied twice")


if __name__ == "__main__":
    unittest.main()
