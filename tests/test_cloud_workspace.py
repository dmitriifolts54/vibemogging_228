import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from vibemogging.cloud import CloudProject, clone_from_cloud, list_shared_projects
from vibemogging.media import edit_media
from vibemogging.repository import Repository
from vibemogging.storage import UserError
from vibemogging.workspace import Workspace
from tests.test_repository import fixture


class CloudFolderTests(unittest.TestCase):
    """Two copies exchange versions through one simulated cloud folder."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.shared = self.root / "Dropbox" / "Vibemogging"
        self.shared.mkdir(parents=True)
        self.alice = Repository(self.root / "alice")
        self.base = fixture(self.alice.store, seconds=48, fps=2)
        self.alice.initialise(self.base, "scenario")
        self.alice_cloud = CloudProject.connect(self.alice, str(self.shared), "alice-mac")

    def shared_root(self):
        return self.shared / f"vibemogging-{self.alice.state['project']['id']}"

    def clone_bob(self):
        CloudProject(self.alice, self.shared, "alice-mac").publish()
        bob = Repository(self.root / "bob")
        clone_from_cloud(bob, str(self.shared), self.alice.state["project"]["id"], "alice-mac", "bob-pc")
        return bob, CloudProject.load(bob)

    def test_publish_clone_edit_receive_merge(self):
        bob, bob_cloud = self.clone_bob()
        self.assertEqual(bob.manifest(), self.alice.manifest())
        self.assertEqual(bob.state["project"], self.alice.state["project"])
        self.assertEqual(list_shared_projects(str(self.shared))[0]["devices"], ["alice-mac", "bob-pc"])

        a = edit_media(self.alice.store, self.alice.manifest(), "fill", 30, 45, value="#ff8800")
        a = edit_media(self.alice.store, a, "mute", 20, 30)
        self.alice.set_working(a)
        self.alice.commit("A")
        b = edit_media(bob.store, bob.manifest(), "fill", 20, 25, value="#0000ff")
        bob.set_working(b)
        bob.commit("B")

        published = bob_cloud.publish()
        self.assertGreater(published["skipped"], 0, "unchanged objects must not be copied again")
        status = self.alice_cloud.status()
        bob_info = next(d for d in status["devices"] if d["device"] == "bob-pc")
        self.assertTrue(bob_info["latest"]["complete"])
        self.assertFalse(bob_info["latest"]["received"])

        result = self.alice_cloud.receive("bob-pc")
        self.assertEqual(result["branches"], ["bob-pc/main"])
        self.assertEqual(self.alice.merge("bob-pc/main")["mode"], "merged")
        merged = self.alice.manifest()
        self.assertEqual(merged["video"][40:50], b["video"][40:50])
        self.assertEqual(merged["video"][60:90], a["video"][60:90])
        self.assertEqual(merged["audio"], a["audio"])
        self.assertTrue(next(d for d in self.alice_cloud.status()["devices"] if d["device"] == "bob-pc")
                        ["latest"]["received"])

        # The merge result travels back as a fast-forward for Bob.
        self.alice_cloud.publish()
        bob_cloud.receive("alice-mac")
        self.assertEqual(bob.merge("alice-mac/main")["mode"], "fast-forward")
        self.assertEqual(bob.manifest(), merged)

    def test_republish_copies_only_new_objects(self):
        first = self.alice_cloud.publish()
        again = self.alice_cloud.publish()
        self.assertGreater(first["copied"], 0)
        self.assertEqual(again["copied"], 0)
        self.assertEqual(again["copied_bytes"], 0)
        edited = edit_media(self.alice.store, self.alice.manifest(), "fill", 1, 2, value="#123456")
        self.alice.set_working(edited)
        self.alice.commit("small")
        third = self.alice_cloud.publish()
        self.assertEqual(third["copied"], 3)  # fill frame, manifest, commit
        snapshots = list((self.shared_root() / "devices" / "alice-mac").glob("snapshot-*.json"))
        self.assertEqual(len(snapshots), 3)

    def test_incomplete_sync_is_reported_and_project_unchanged(self):
        bob, bob_cloud = self.clone_bob()
        edited = edit_media(bob.store, bob.manifest(), "fill", 3, 4, value="#00ff00")
        bob.set_working(edited)
        bob.commit("B")
        bob_cloud.publish()
        fill_frame = edited["video"][6]
        missing = self.shared_root() / "objects" / "video" / fill_frame[:2] / f"{fill_frame}.z"
        held = missing.read_bytes()
        missing.unlink()
        before = json.loads((self.alice.root / "state.json").read_text())
        latest = next(d for d in self.alice_cloud.status()["devices"] if d["device"] == "bob-pc")["latest"]
        self.assertFalse(latest["complete"])
        self.assertEqual(latest["missing_files"], 1)
        with self.assertRaises(UserError):
            self.alice_cloud.receive("bob-pc")
        self.assertEqual(json.loads((self.alice.root / "state.json").read_text()), before)
        missing.write_bytes(held)
        self.alice_cloud.receive("bob-pc")
        self.assertIn("bob-pc/main", self.alice.state["branches"])

    def test_tampered_object_and_truncated_snapshot_are_rejected(self):
        bob, bob_cloud = self.clone_bob()
        edited = edit_media(bob.store, bob.manifest(), "fill", 3, 4, value="#00ff00")
        bob.set_working(edited)
        bob.commit("B")
        bob_cloud.publish()
        frame = edited["video"][6]
        path = self.shared_root() / "objects" / "video" / frame[:2] / f"{frame}.z"
        original = path.read_bytes()
        import zlib
        tampered = zlib.compress(b"\x01" * 24, 6)
        path.write_bytes(tampered.ljust(len(original), b"\0")[:len(original)])
        with self.assertRaises(UserError):
            self.alice_cloud.receive("bob-pc")
        self.assertNotIn("bob-pc/main", self.alice.state["branches"])
        path.write_bytes(original)
        # A newer snapshot that is only partly synced falls back to the previous one.
        newest = sorted((self.shared_root() / "devices" / "bob-pc").glob("snapshot-*.json"))[-1]
        broken = newest.with_name("snapshot-9999-partial.json")
        broken.write_text(newest.read_text()[:100])
        latest = next(d for d in self.alice_cloud.status()["devices"] if d["device"] == "bob-pc")
        self.assertEqual(latest["latest"]["file"], newest.name)
        self.assertEqual(latest["unreadable"], 1)
        self.alice_cloud.receive("bob-pc")

    def test_device_names_and_projects_are_checked(self):
        with self.assertRaises(UserError):
            CloudProject.connect(self.alice, "relative/path", "x")
        with self.assertRaises(UserError):
            CloudProject.connect(self.alice, str(self.shared), "bad name")
        other = Repository(self.root / "other")
        other.initialise(fixture(other.store), "other")
        # Different project, same folder: separate directory, no interference.
        CloudProject.connect(other, str(self.shared), "alice-mac").publish()
        self.assertEqual(len(list_shared_projects(str(self.shared))), 2)
        bob = Repository(self.root / "bob")
        with self.assertRaises(UserError):
            clone_from_cloud(bob, str(self.shared), self.alice.state["project"]["id"], "alice-mac", "alice-mac")

    def test_shared_folder_contains_no_mutable_state(self):
        self.alice_cloud.publish()
        names = {p.name for p in self.shared_root().rglob("*") if p.is_file()}
        self.assertNotIn("state.json", names)
        self.assertFalse(any(name.endswith((".sqlite", ".db", ".tmp")) for name in names))


class BundleFormatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = Repository(self.root / "a")
        self.repo.initialise(fixture(self.repo.store), "demo")

    def test_v2_layout_and_v1_backwards_import(self):
        archive = self.root / "v2.vmrepo"
        self.repo.export_bundle(archive)
        with zipfile.ZipFile(archive) as bundle:
            index = json.loads(bundle.read("index.json"))
            self.assertEqual(index["format"], 2)
            self.assertIn("recipes.json", bundle.namelist())
        v1 = self.root / "v1.vmrepo"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(v1, "w") as target:
            header = {key: index[key] for key in ("project", "branches", "head")}
            target.writestr("index.json", json.dumps({"format": 1, **header}))
            for name in source.namelist():
                if name.startswith("objects/"):
                    target.writestr(name, source.read(name))
        clone = Repository(self.root / "clone")
        clone.import_bundle(v1)
        self.assertEqual(clone.manifest(), self.repo.manifest())

    def test_missing_object_in_v2_is_rejected_before_refs_change(self):
        archive = self.root / "v2.vmrepo"
        self.repo.export_bundle(archive)
        broken = self.root / "broken.vmrepo"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(broken, "w") as target:
            for name in source.namelist():
                if not name.startswith("objects/audio/"):
                    target.writestr(name, source.read(name))
        clone = Repository(self.root / "clone")
        with self.assertRaises(UserError):
            clone.import_bundle(broken)
        self.assertIsNone(clone.state["project"])

    def test_unknown_format_rejected(self):
        bad = self.root / "future.vmrepo"
        with zipfile.ZipFile(bad, "w") as target:
            target.writestr("index.json", json.dumps({"format": 99}))
        with self.assertRaises(UserError):
            Repository(self.root / "x").import_bundle(bad)


class WorkspaceTests(unittest.TestCase):
    def test_create_list_reopen_and_register_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root / "ws")
            first = workspace.create("  Отпуск   2026 ")
            self.assertEqual(first["name"], "Отпуск 2026")
            legacy = Repository(root / "legacy")
            legacy.initialise(fixture(legacy.store), "old.mov")
            registered = workspace.register(root / "legacy")
            self.assertEqual(registered["name"], "old.mov")
            self.assertEqual(workspace.register(root / "legacy")["id"], registered["id"])
            workspace.touch(first["id"])
            reopened = Workspace(root / "ws")
            listing = reopened.listing()
            self.assertEqual([p["id"] for p in listing], [first["id"], registered["id"]])
            self.assertFalse(listing[0]["initialised"])
            self.assertTrue(listing[1]["initialised"])
            reopened.forget(registered["id"])
            self.assertTrue((root / "legacy" / "state.json").exists(), "forgetting never deletes data")
            with self.assertRaises(UserError):
                reopened.create("")
            with self.assertRaises(UserError):
                reopened.register(root / "nothing")


if __name__ == "__main__":
    unittest.main()
