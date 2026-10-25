"""Repeatable disk-size measurement of legacy RGB objects versus compact import."""

import argparse
import json
from pathlib import Path
import subprocess
import time

from . import media
from .storage import Store, UserError, atomic_write, canonical


def benchmark(root: Path, source: Path | None = None, seconds=12):
    root.mkdir(parents=True, exist_ok=False)
    if source is None:
        source = root / "input.mp4"
        subprocess.run(media.command(
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=25:duration={seconds},noise=alls=6:allf=t+u",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source), check=True, capture_output=True)
    reports, manifests = {}, {}
    for name, compact in (("legacy", False), ("compact", True)):
        store = Store(root / name)
        try:
            start = time.monotonic()
            manifest = media.import_media(store, source, compact=compact)
            manifests[name] = manifest
            reports[name] = {**store.stats(), "import_seconds": round(time.monotonic() - start, 3)}
            if compact:
                before = store.stats()["total_bytes"]
                edited = media.edit_media(store, manifest, "brighten", 0,
                                           len(manifest["video"]) * manifest["fps"][1] / manifest["fps"][0])
                store.flush()
                reports[name]["brightness_added_bytes"] = store.stats()["total_bytes"] - before
                legacy = Store(root / "legacy")
                try:
                    # Validate all reconstructed frames, including seeking and LUT results.
                    table = bytes(min(255, round(v * 1.2)) for v in range(256))
                    for original, changed in zip(manifest["video"], edited["video"]):
                        if store.get("video", original) != legacy.get("video", original):
                            raise UserError("Исходные кадры не совпали побайтно.")
                        if store.get("video", changed) != legacy.get("video", original).translate(table):
                            raise UserError("Изменённые кадры не совпали побайтно.")
                    reports[name]["verified_all_frames"] = True
                finally:
                    legacy.close()
            print(name + ": " + json.dumps(reports[name]), flush=True)
        finally:
            store.close()
    if manifests["legacy"] != manifests["compact"]:
        raise UserError("Манифесты старого и компактного импорта различаются.")
    report = {"source_bytes": source.stat().st_size, "frames": len(manifests["compact"]["video"]),
              "width": manifests["compact"]["width"], "height": manifests["compact"]["height"],
              "fps": manifests["compact"]["fps"], "measurements": reports,
              "disk_reduction_factor": round(reports["legacy"]["total_bytes"] / reports["compact"]["total_bytes"], 3)}
    atomic_write(root / "report.json", canonical(report))
    return report


def main():
    parser = argparse.ArgumentParser(description="Сравнить размер старого и компактного видеохранилища")
    parser.add_argument("--output-dir", type=Path, required=True, help="Новая папка для результатов")
    parser.add_argument("--source", type=Path, help="Свое видео вместо генерируемого тестового ролика")
    parser.add_argument("--seconds", type=int, default=12, help="Длительность генерируемого ролика")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120:
        parser.error("длительность генерируемого примера — от 1 до 120 секунд")
    try:
        report = benchmark(args.output_dir.resolve(), args.source, args.seconds)
    except (UserError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Ошибка проверки: {error}\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
