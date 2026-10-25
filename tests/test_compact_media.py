"""Storage size, exact reconstruction, bounded readers and combined replacement."""

from copy import deepcopy
from fractions import Fraction
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from vibemogging import media
from vibemogging.storage import Store, UserError, digest


@unittest.skipUnless(media.tools_available(), "FFmpeg and ffprobe are required")
class CompactMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.source = cls.root / "source.mp4"
        subprocess.run(media.command("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=4",
                                     "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
                                     "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p",
                                     "-c:a", "aac", "-shortest", cls.source), check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.compact = Store(self.path / "compact", cache_bytes=100_000)
        self.legacy = Store(self.path / "legacy")
        self.addCleanup(self.legacy.close)
        self.addCleanup(self.compact.close)

    def import_pair(self):
        current = media.import_media(self.compact, self.source)
        old = media.import_media(self.legacy, self.source, compact=False)
        self.assertEqual(current, old)
        return current

    def test_import_keeps_source_once_and_no_rgb_payloads(self):
        manifest = self.import_pair()
        for ref in manifest["video"]:
            self.assertFalse(self.compact.path(ref).exists())
            self.assertEqual(self.compact.recipe(ref)["type"], "source")
        stats = self.compact.stats()
        self.assertEqual(stats["assets"], 1)
        self.assertEqual(stats["asset_bytes"], self.source.stat().st_size)
        self.assertLess(stats["total_bytes"], self.legacy.stats()["total_bytes"])

    def test_random_and_sequential_access_are_byte_exact(self):
        manifest = self.import_pair()
        for index in (35, 3, 18, 19, 20, 21, 0, 39, 5):
            ref = manifest["video"][index]
            self.assertEqual(self.compact.get("video", ref), self.legacy.get("video", ref))
            self.assertLessEqual(self.compact.stats()["frame_cache_bytes"], 100_000)
        self.assertLessEqual(len(self.compact._decoders), 2)

    def test_brightness_stores_lut_and_preserves_exact_composition(self):
        manifest = self.import_pair()
        bright = media.edit_media(self.compact, manifest, "brighten", 0, 4)
        changed = media.edit_media(self.compact, bright, "darken", 0, 4)
        self.compact.flush()
        first = bytes(min(255, round(v * 1.2)) for v in range(256))
        second = bytes(round(v * .8) for v in range(256))
        for original, ref in zip(manifest["video"], changed["video"]):
            expected = self.legacy.get("video", original).translate(first).translate(second)
            self.assertEqual(self.compact.get("video", ref), expected)
            self.assertFalse(self.compact.path(ref).exists())
            self.assertEqual(self.compact.recipe(ref)["type"], "lut")
            self.assertEqual(self.compact.recipe(ref)["base"], original)
        self.assertEqual(self.compact.stats()["assets"], 1)

    def test_export_plan_includes_dependencies_without_decoding(self):
        manifest = media.import_media(self.compact, self.source)
        edited = media.edit_media(self.compact, manifest, "brighten", 0, 4)
        plan = self.compact.export_plan(("video", ref) for ref in edited["video"])
        self.assertEqual(len(plan["assets"]), 1)
        self.assertTrue(all(item["kind"] == "lut" for item in plan["objects"]))
        self.assertGreater(len(plan["recipes"]), len(set(manifest["video"])))
        restored = Store(self.path / "restored")
        self.addCleanup(restored.close)
        for asset in plan["assets"]:
            path = self.compact.asset_path(asset)
            with path.open("rb") as source:
                restored.import_asset(source, asset, path.stat().st_size)
        for item in plan["objects"]:
            self.assertEqual(restored.put(item["kind"], self.compact.get(item["kind"], item["id"])), item["id"])
        for ref, recipe in plan["recipes"].items():
            restored.import_recipe(ref, recipe)
        restored.flush()
        for ref in edited["video"]:
            self.assertEqual(restored.get("video", ref), self.compact.get("video", ref))

    def test_legacy_normalization_and_source_offset_reconstruct(self):
        for offset in (.3, .17):
            refs = media.decode_frames(self.compact, self.source, 160, 90, Fraction(10),
                                        offset=offset, count=10, normalization=1)
            old = media.decode_frames(self.legacy, self.source, 160, 90, Fraction(10),
                                       offset=offset, count=10, compact=False, normalization=1)
            self.assertEqual(refs, old)
            for ref in refs:
                self.assertEqual(self.compact.get("video", ref), self.legacy.get("video", ref))

    def test_combined_replacement_is_atomic_on_missing_audio(self):
        manifest = media.import_media(self.compact, self.source)
        original = deepcopy(manifest)
        silent = self.path / "silent.mp4"
        subprocess.run(media.command("-i", self.source, "-an", "-c:v", "copy", silent), check=True, capture_output=True)
        with self.assertRaises(UserError):
            media.edit_media(self.compact, manifest, "replace_av", 0, 1, silent)
        self.assertEqual(manifest, original)

    def test_combined_replacement_matches_two_separate_edits(self):
        manifest = media.import_media(self.compact, self.source)
        combined = media.edit_media(self.compact, manifest, "replace_av", 1, 2, self.source, offset=2)
        video = media.edit_media(self.compact, manifest, "replace_video", 1, 2, self.source, offset=2)
        separate = media.edit_media(self.compact, video, "replace_audio", 1, 2, self.source, offset=2)
        self.assertEqual(combined, separate)

    def test_asset_checksum_and_recipe_cycles_are_rejected(self):
        with self.assertRaises(UserError):
            self.compact.import_asset(io.BytesIO(b"corrupt"), "a" * 64, 7)
        self.assertEqual(self.compact.stats()["assets"], 0)
        table = self.compact.put("lut", bytes(range(256)))
        self.compact.import_recipe("a" * 64, {"type": "lut", "base": "b" * 64, "table": table})
        self.compact.import_recipe("b" * 64, {"type": "lut", "base": "a" * 64, "table": table})
        with self.assertRaises(UserError):
            self.compact.get("video", "a" * 64)

    def test_reopened_index_is_self_contained(self):
        original = self.path / "temporary-input.mp4"
        shutil.copyfile(self.source, original)
        manifest = media.import_media(self.compact, original)
        expected = self.compact.get("video", manifest["video"][30])
        self.compact.close()
        original.unlink()
        reopened = Store(self.path / "compact")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get("video", manifest["video"][30]), expected)

    def test_lossless_rgb_packing_preserves_all_content_ids(self):
        manifest = self.import_pair()
        packed = self.path / "packed.mkv"
        process = subprocess.Popen(media.command(
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "160x90", "-r", "10", "-i", "pipe:0",
            "-an", "-c:v", "libx264rgb", "-crf", "0", "-preset", "fast", "-g", "20", packed),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for ref in manifest["video"]:
            process.stdin.write(self.legacy.get("video", ref))
        process.stdin.close()
        errors = process.stderr.read()
        self.assertEqual(process.wait(), 0, errors)
        process.stderr.close()
        destination = Store(self.path / "lossless-packed")
        self.addCleanup(destination.close)
        refs = media.decode_frames(destination, packed, 160, 90, Fraction(10))
        self.assertEqual(refs, manifest["video"])
        for ref in refs:
            self.assertEqual(destination.get("video", ref), self.legacy.get("video", ref))

    def test_variable_frame_rate_source_is_exact_after_random_seeks(self):
        source = self.path / "variable-frame-rate.mkv"
        subprocess.run(media.command("-i", self.source, "-vf", "select=not(eq(mod(n\\,5)\\,1))",
                                     "-fps_mode", "vfr", "-an", "-c:v", "libx264", "-g", "10", source),
                       check=True, capture_output=True)
        current = media.import_media(self.compact, source)
        old = media.import_media(self.legacy, source, compact=False)
        self.assertEqual(current, old)
        count = len(current["video"])
        for index in (count - 1, 0, count // 2, 2, count - 3, 1):
            ref = current["video"][index]
            self.assertEqual(self.compact.get("video", ref), self.legacy.get("video", ref))

    def test_fractional_rate_and_subframe_offset_are_exact(self):
        fps = Fraction(30000, 1001)
        current = media.decode_frames(self.compact, self.source, 128, 72, fps, offset=.137, count=60)
        old = media.decode_frames(self.legacy, self.source, 128, 72, fps, offset=.137, count=60, compact=False)
        self.assertEqual(current, old)
        for index in (59, 1, 28, 29, 30, 57, 0):
            ref = current[index]
            self.assertEqual(self.compact.get("video", ref), self.legacy.get("video", ref))

    def test_verified_reimport_repairs_corrupted_asset(self):
        manifest = self.import_pair()
        ref = manifest["video"][30]
        asset_id = self.compact.recipe(ref)["asset"]
        self.compact.get("video", manifest["video"][0])
        self.compact.asset_path(asset_id).write_bytes(b"damaged test asset")
        with self.source.open("rb") as stream:
            self.compact.import_asset(stream, asset_id, self.source.stat().st_size)
        self.assertEqual(self.compact.get("video", ref), self.legacy.get("video", ref))

    def test_existing_legacy_repository_is_not_modified_by_reading(self):
        ref = self.legacy.put("video", b"\0" * 24)
        before = sorted(p.relative_to(self.legacy.root) for p in self.legacy.root.rglob("*") if p.is_file())
        self.assertEqual(self.legacy.get("video", ref), b"\0" * 24)
        self.legacy.stats()
        after = sorted(p.relative_to(self.legacy.root) for p in self.legacy.root.rglob("*") if p.is_file())
        self.assertEqual(before, after)
        self.assertFalse((self.legacy.root / "sources.sqlite3").exists())

    def test_composed_effects_can_return_to_original_without_a_cycle(self):
        original = self.compact.put("video", bytes([5]) * 24)
        brighten = bytes(min(255, round(v * 1.2)) for v in range(256))
        darken = bytes(round(v * .8) for v in range(256))
        brighter = self.compact.put_video_lut(original, brighten)
        restored = self.compact.put_video_lut(brighter, darken)
        self.assertEqual(restored, original)
        self.assertEqual(self.compact.get("video", restored), bytes([5]) * 24)


if __name__ == "__main__":
    unittest.main()
