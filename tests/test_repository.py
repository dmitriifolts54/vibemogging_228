from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from vibemogging.media import edit_media, interval
from vibemogging.merge import (AUDIO_BLOCK, FRAME_BYTES, SAMPLE_RATE, diff_manifests,
                               merge_manifests, read_audio, write_audio)
from vibemogging.repository import Repository
from vibemogging.storage import Store, UserError


def fixture(store, seconds=2, fps=4):
    frames = [store.put("video", bytes([20 + i % 200]) * 24) for i in range(seconds * fps)]
    count = seconds * SAMPLE_RATE
    audio = [store.put("audio", struct.pack("<hh", 1000, 1000) * min(AUDIO_BLOCK, count - i))
             for i in range(0, count, AUDIO_BLOCK)]
    return {"width": 4, "height": 2, "fps": [fps, 1], "video": frames, "audio": audio, "samples": count}


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = Repository(self.root / "a")
        self.base = fixture(self.repo.store)
        self.initial = self.repo.initialise(self.base, "demo")

    def video_edit(self, first, last, color):
        manifest = self.repo.manifest()
        ref = self.repo.store.put("video", bytes(color) * 8)
        manifest["video"][first:last] = [ref] * (last - first)
        self.repo.set_working(manifest)

    def test_requested_45_second_scenario(self):
        repo = Repository(self.root / "scenario")
        original = fixture(repo.store, seconds=48, fps=2)
        base = repo.initialise(original, "scenario")
        repo.branch("alice")
        a = edit_media(repo.store, repo.manifest(), "fill", 30, 45, value="#cc3300")
        a = edit_media(repo.store, a, "mute", 20, 30)
        repo.set_working(a)
        alice = repo.commit("Alice: video 30–45, audio 20–30")
        repo.checkout("main")
        repo.branch("bob")
        b = edit_media(repo.store, repo.manifest(), "fill", 20, 25, value="#0066cc")
        repo.set_working(b)
        bob = repo.commit("Bob: video 20–25")
        repo.checkout("alice")
        result = repo.merge("bob")
        self.assertEqual(result["mode"], "merged")
        merged = repo.manifest()
        self.assertEqual(merged["video"][40:50], b["video"][40:50])
        self.assertEqual(merged["video"][60:90], a["video"][60:90])
        self.assertEqual(merged["audio"], a["audio"])
        for index in list(range(40)) + list(range(50, 60)) + list(range(90, 96)):
            self.assertEqual(merged["video"][index], original["video"][index])
        self.assertEqual(repo.get_commit(repo.head)["parents"], [alice, bob])
        self.assertEqual(repo.merge("bob")["mode"], "unchanged")
        self.assertEqual(repo.merge_base(alice, bob), base)
        self.assertFalse(repo.dirty())

    def test_independent_audio_edits_in_same_block_and_channels(self):
        ours, theirs = deepcopy(self.base), deepcopy(self.base)
        write_audio(self.repo.store, ours, 4, struct.pack("<h", 2000))
        write_audio(self.repo.store, theirs, 6, struct.pack("<h", 3000))
        merged, conflicts = merge_manifests(self.repo.store, self.base, ours, theirs)
        self.assertEqual(conflicts, [])
        self.assertEqual(read_audio(self.repo.store, merged, 4, 8), struct.pack("<hh", 2000, 3000))
        changed = diff_manifests(self.repo.store, self.base, merged)
        self.assertEqual(changed["audio_samples"], 1)
        self.assertEqual(changed["video_frames"], 0)

    def test_video_conflict_resolution_and_merge_parents(self):
        self.repo.branch("alice")
        self.video_edit(2, 4, [200, 0, 0])
        ours = self.repo.commit("red")
        self.repo.checkout("main")
        self.video_edit(2, 4, [0, 0, 200])
        theirs = self.repo.commit("blue")
        self.assertEqual(self.repo.merge("alice")["mode"], "conflicts")
        pending = self.repo.status()["merge"]
        self.assertEqual(len(pending["conflicts"]), 1)
        conflict = pending["conflicts"][0]
        self.assertEqual(conflict["start_time"], .5)
        with self.assertRaises(UserError):
            self.repo.commit("unresolved")
        with self.assertRaises(UserError):
            self.repo.checkout("alice")
        self.repo.resolve(conflict["id"], "theirs")
        self.repo.commit("resolved")
        self.assertEqual(self.repo.get_commit(self.repo.head)["parents"], [theirs, ours])
        self.assertEqual(self.repo.manifest()["video"], self.repo.manifest(ours)["video"])

    def test_audio_conflict_resolution_preserves_other_changes(self):
        self.repo.branch("alice")
        a = self.repo.manifest()
        write_audio(self.repo.store, a, 4, struct.pack("<h", 2000))
        write_audio(self.repo.store, a, 20, struct.pack("<h", 2222))
        self.repo.set_working(a)
        self.repo.commit("alice")
        self.repo.checkout("main")
        b = self.repo.manifest()
        write_audio(self.repo.store, b, 4, struct.pack("<h", 3000))
        write_audio(self.repo.store, b, 30, struct.pack("<h", 3333))
        self.repo.set_working(b)
        self.repo.commit("bob")
        self.repo.merge("alice")
        conflict = self.repo.state["merge"]["conflicts"][0]
        self.repo.resolve(conflict["id"], "base")
        self.repo.commit("choose base")
        result = self.repo.manifest()
        self.assertEqual(read_audio(self.repo.store, result, 4, 6), struct.pack("<h", 1000))
        self.assertEqual(read_audio(self.repo.store, result, 20, 22), struct.pack("<h", 2222))
        self.assertEqual(read_audio(self.repo.store, result, 30, 32), struct.pack("<h", 3333))

    def test_drafts_survive_switch_and_restart(self):
        self.video_edit(0, 1, [2, 3, 4])
        edited = self.repo.manifest_id()
        self.repo.branch("draft-copy")
        self.video_edit(2, 3, [5, 6, 7])
        other = self.repo.manifest_id()
        self.repo.checkout("main")
        self.assertEqual(self.repo.manifest_id(), edited)
        reloaded = Repository(self.root / "a")
        reloaded.checkout("draft-copy")
        self.assertEqual(reloaded.manifest_id(), other)
        self.assertTrue(reloaded.dirty())

    def test_restore_preserves_history(self):
        self.video_edit(0, 2, [20, 21, 22])
        changed = self.repo.commit("change")
        self.repo.restore(self.initial)
        restored = self.repo.commit("restore")
        self.assertEqual(self.repo.manifest(), self.base)
        self.assertEqual(self.repo.get_commit(restored)["parents"], [changed])
        self.assertEqual(len(self.repo.history()), 3)

    def test_cancel_merge_returns_to_head(self):
        self.repo.branch("alice")
        self.video_edit(0, 1, [1, 2, 3])
        self.repo.commit("A")
        self.repo.checkout("main")
        self.video_edit(0, 1, [3, 2, 1])
        head = self.repo.commit("B")
        self.repo.merge("alice")
        self.repo.cancel_merge()
        self.assertEqual(self.repo.head, head)
        self.assertFalse(self.repo.dirty())

    def test_pixel_resolver_hook(self):
        ours, theirs = deepcopy(self.base), deepcopy(self.base)
        ours["video"][0] = self.repo.store.put("video", b"\x80" * 24)
        theirs["video"][0] = self.repo.store.put("video", b"\x90" * 24)
        calls = []

        def fuse(base, left, right, context):
            calls.append(context)
            return b"\x88" * 24

        merged, conflicts = merge_manifests(self.repo.store, self.base, ours, theirs, fuse)
        self.assertEqual(conflicts, [])
        self.assertEqual(self.repo.store.get("video", merged["video"][0]), b"\x88" * 24)
        self.assertEqual(calls[0]["frame"], 0)

    def test_portable_clone_exchange_and_merge(self):
        archive = self.root / "clone.vmrepo"
        self.repo.export_bundle(archive)
        second = Repository(self.root / "b")
        second.import_bundle(archive)
        self.assertEqual(second.manifest(), self.repo.manifest())
        a = edit_media(self.repo.store, self.repo.manifest(), "fill", 0, .5, value="#aa3300")
        self.repo.set_working(a)
        self.repo.commit("A")
        b = edit_media(second.store, second.manifest(), "fill", 1, 1.5, value="#0033aa")
        second.set_working(b)
        second.commit("B")
        second.export_bundle(archive)
        self.repo.import_bundle(archive)
        self.repo.merge("peer/main")
        final = self.repo.manifest()
        self.assertEqual(final["video"][:2], a["video"][:2])
        self.assertEqual(final["video"][4:6], b["video"][4:6])
        self.assertEqual(final["audio"], self.base["audio"])

    def test_corrupt_bundle_does_not_modify_history(self):
        archive = self.root / "bundle.vmrepo"
        self.repo.export_bundle(archive)
        broken = self.root / "broken.vmrepo"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(broken, "w") as target:
            for name in source.namelist():
                raw = source.read(name)
                target.writestr(name, b"invalid" if name.startswith("objects/video/") else raw)
        second = Repository(self.root / "b")
        with self.assertRaises(UserError):
            second.import_bundle(broken)
        self.assertIsNone(second.state["project"])

    def test_dirty_merge_and_export_rejected(self):
        self.video_edit(0, 1, [1, 2, 3])
        with self.assertRaises(UserError):
            self.repo.merge("main")
        with self.assertRaises(UserError):
            self.repo.export_bundle(self.root / "dirty.vmrepo")

    def test_repeated_exchange_does_not_reexport_incoming_branches(self):
        archive = self.root / "exchange.vmrepo"
        self.repo.export_bundle(archive)
        second = Repository(self.root / "second")
        second.import_bundle(archive)
        self.repo.import_bundle(archive, peer="bob")
        self.repo.export_bundle(archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(set(json.loads(bundle.read("index.json"))["branches"]), {"main"})
        second.import_bundle(archive, peer="alice")
        second.export_bundle(archive)
        self.repo.import_bundle(archive, peer="bob")
        self.assertEqual(set(self.repo.state["branches"]), {"main", "bob/main"})

    def test_bundle_cannot_overwrite_owned_branch(self):
        self.repo.branch("bob/main")
        self.repo.checkout("main")
        archive = self.root / "collision.vmrepo"
        self.repo.export_bundle(archive)
        with self.assertRaises(UserError):
            self.repo.import_bundle(archive, peer="bob")

    def test_ranges_and_names_validated(self):
        for start, end in ((-1, 1), (0, 3), (1, 1), (0, float("nan")), (float("inf"), 1)):
            with self.assertRaises(UserError):
                interval(self.base, start, end)
        with self.assertRaises(UserError):
            self.repo.branch("../escape")
        with self.assertRaises(UserError):
            self.repo.branch("working")


if __name__ == "__main__":
    unittest.main()
