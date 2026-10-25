"""Generate the agreed two-computer acceptance scenario with real media."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

from . import media
from .repository import Repository
from .storage import UserError, atomic_write, canonical


def build_demo(root: Path):
    if not media.tools_available():
        raise UserError("Для примера установите FFmpeg и ffprobe.")
    root = root.resolve()
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise UserError("Выберите новую папку --output-dir: существующие данные не перезаписываются.") from error
    print("Создаю 48-секундный тестовый ролик…", flush=True)
    source = root / "source.mp4"
    process = subprocess.run(media.command(
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=4:duration=48",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=48",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source),
        capture_output=True)
    if process.returncode:
        raise UserError("Не удалось создать пример: " + process.stderr[:1000].decode(errors="replace"))
    alice = Repository(root / "alice")
    original = media.import_media(alice.store, source)
    base = alice.initialise(original, "Два участника · пример V1")
    initial = root / "initial.vmrepo"
    alice.export_bundle(initial)
    bob = Repository(root / "bob")
    bob.import_bundle(initial)
    print("Применяю независимые изменения Alice и Bob…", flush=True)
    a = media.edit_media(alice.store, alice.manifest(), "fill", 30, 45, value="#d65234")
    a = media.edit_media(alice.store, a, "gain", 20, 30, value=.25)
    alice.set_working(a)
    alice_commit = alice.commit("Alice: видео 00:30–00:45, звук 00:20–00:30", "Alice")
    b = media.edit_media(bob.store, bob.manifest(), "fill", 20, 25, value="#348bdd")
    bob.set_working(b)
    bob_commit = bob.commit("Bob: видео 00:20–00:25", "Bob")
    bob.export_bundle(root / "bob.vmrepo")
    alice.import_bundle(root / "bob.vmrepo", "bob")
    merged = alice.merge("bob/main")
    if merged["mode"] != "merged":
        raise UserError("Пример неожиданно завершился конфликтом.")
    final = alice.manifest()
    fps = final["fps"][0] / final["fps"][1]
    expected = list(original["video"])
    expected[round(20 * fps):round(25 * fps)] = b["video"][round(20 * fps):round(25 * fps)]
    expected[round(30 * fps):round(45 * fps)] = a["video"][round(30 * fps):round(45 * fps)]
    if final["video"] != expected or final["audio"] != a["audio"]:
        raise UserError("Проверка целостности объединённого результата не пройдена.")
    print("Слияние без конфликтов подтверждено. Экспортирую результат…", flush=True)
    media.render(alice.store, final, root / "merged.mp4")
    alice.export_bundle(root / "merged.vmrepo")
    report = {"base": base, "alice": alice_commit, "bob": bob_commit,
              "merge": alice.head, "verified": True, "duration": len(final["video"]) / fps,
              "video_ranges": [[20, 25], [30, 45]], "audio_range": [20, 30],
              "alice_repo": str(alice.root), "bob_repo": str(bob.root)}
    atomic_write(root / "report.json", canonical(report))
    return report


def main():
    parser = argparse.ArgumentParser(description="Создать воспроизводимый пример слияния двух видеорепозиториев")
    parser.add_argument("--output-dir", type=Path, required=True, help="Новая папка для двух репозиториев и результата")
    args = parser.parse_args()
    try:
        report = build_demo(args.output_dir)
    except (UserError, OSError) as error:
        parser.exit(1, f"Ошибка: {error}\n")
    print(f"Проверено: {args.output_dir.resolve() / 'merged.mp4'}", flush=True)
    print(f"Для интерфейса: python3 -m vibemogging --data-dir {report['alice_repo']}", flush=True)


if __name__ == "__main__":
    main()
