"""Copy a legacy repository into a new compact folder, keeping every raw ID.

    python3 -m vibemogging.maintenance migrate OLD_DIR NEW_DIR [--source original.mov]

The original folder is only read. The new folder is built under a temporary
name next to the target and renamed into place after every frame and sample has
been verified through the new Store. Legacy RGB frames are packed into short
lossless `libx264rgb -crf 0` clips; each clip is decoded back and compared by
content ID before its frames are registered. Frames that do not round-trip are
kept as ordinary lossless objects. Optionally, the original source file can
stand in for the first imported version if it reproduces every frame ID.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from . import media
from .locking import DirectoryLock
from .repository import Repository
from .storage import Store, UserError, atomic_write, canonical, digest
from .video_sources import video_command
from .workspace import folder_size

COMPACT_API = ("add_asset", "register_source_frame", "recipe", "flush", "close")
CLIP_FRAMES = 240


def require_compact_store(store):
    missing = [name for name in COMPACT_API if not hasattr(store, name)]
    if missing:
        raise UserError("Для конвертации нужно компактное хранилище (ветка feature/compact-media-storage). "
                        "Обновите приложение.")


def manifests_to_keep(repo: Repository):
    """Every manifest referenced by history, drafts and a pending merge, oldest first."""
    commits = {}
    queue = list(repo.state["branches"].values())
    pending = repo.state["merge"]
    if pending:
        queue += [pending["base"], *pending["parents"]]
    while queue:
        ref = queue.pop()
        if ref not in commits:
            commits[ref] = repo.store.get_json("commit", ref)
            queue.extend(commits[ref]["parents"])
    ordered = [c["manifest"] for _, c in sorted(commits.items(), key=lambda item: item[1]["created"])]
    ordered += list(repo.state["drafts"].values())
    return commits, list(dict.fromkeys(ordered))


def encode_clip(frames, width, height, fps: Fraction, output: Path, read_frame, preset="medium"):
    """Stream RGB frames into a lossless clip; one keyframe per clip bounds random access."""
    args = media.command("-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
                         "-framerate", f"{fps.numerator}/{fps.denominator}", "-i", "pipe:0",
                         "-an", "-c:v", "libx264rgb", "-crf", "0", "-preset", preset,
                         "-g", str(len(frames)), "-threads", "2", output)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
        try:
            for frame_id in frames:
                process.stdin.write(read_frame(frame_id))
            process.stdin.close()
            code = process.wait()
        except BrokenPipeError:
            code = process.wait()
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if code:
            errors.seek(0)
            raise UserError("FFmpeg не смог упаковать кадры: " + errors.read(800).decode(errors="replace"))


def decoded_ids(path: Path, profile: dict, count):
    """Content IDs of the frames the Store's own decoder pipeline yields (streamed)."""
    args = video_command(path, profile)
    size = profile["width"] * profile["height"] * 3
    ids = []
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
        try:
            while len(ids) <= count:
                frame = media.read_exact(process.stdout, size)
                if len(frame) != size:
                    break
                ids.append(digest("video", frame))
            process.stdout.close()
            process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return ids


def migrate(source: Path, target: Path, original: Path | None = None, progress=lambda text, value: None,
            clip_frames=CLIP_FRAMES, preset="medium", store_factory=Store) -> dict:
    source, target = Path(source).expanduser().resolve(), Path(target).expanduser().resolve()
    if not (source / "state.json").is_file():
        raise UserError("В исходной папке нет проекта Vibemogging.")
    if target.exists():
        raise UserError("Папка назначения уже существует. Укажите новую папку — данные не перезаписываются.")
    if target == source or source in target.parents:
        raise UserError("Новая папка не может находиться внутри исходного проекта.")
    if not media.tools_available():
        raise UserError("Для конвертации нужны FFmpeg и ffprobe.")
    started = time.time()
    lock = DirectoryLock(source)  # no server may change the old project while it is copied
    partial = target.parent / f".{target.name}.partial-{uuid.uuid4().hex[:8]}"
    try:
        old = Repository(source)
        if not old.state["project"]:
            raise UserError("Исходный проект пуст: конвертировать нечего.")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.mkdir()
        new_store = store_factory(partial)
        require_compact_store(new_store)
        try:
            report = _copy(old, new_store, partial, original, progress, clip_frames, preset)
            progress("Проверка всех кадров и звука…", .95)
            _verify(old, new_store, report, progress)
            new_store.flush()
        finally:
            new_store.close()
        # Same refs, drafts, pending conflicts and incoming branches; device settings are not copied.
        atomic_write(partial / "state.json", canonical(old.state))
        os.replace(partial, target)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    finally:
        lock.close()
    report.update(before_bytes=folder_size(source), after_bytes=folder_size(target),
                  seconds=round(time.time() - started, 1), source=str(source), target=str(target))
    progress("Готово", 1)
    return report


def _copy(old, store, partial, original, progress, clip_frames, preset):
    commits, manifest_ids = manifests_to_keep(old)
    manifests = {mid: old.store.get_json("manifest", mid) for mid in manifest_ids}
    first = manifests[manifest_ids[0]]
    width, height, fps = first["width"], first["height"], Fraction(*first["fps"])
    for commit_id in commits:
        store.put("commit", old.store.get("commit", commit_id))
    audio = []
    for manifest_id, manifest in manifests.items():
        store.put("manifest", old.store.get("manifest", manifest_id))
        audio.extend(manifest["audio"])
    for audio_id in dict.fromkeys(audio):
        store.put("audio", old.store.get("audio", audio_id))  # exact PCM, unchanged
    frames = list(dict.fromkeys(ref for mid in manifest_ids for ref in manifests[mid]["video"]))
    report = {"frames": len(frames), "from_original": 0, "packed": 0, "fallback": 0, "clips": 0,
              "kept_recipes": 0, "commits": len(commits), "manifests": len(manifests),
              "audio_blocks": len(set(audio)), "original_matched": None}
    # Frames that already have source/LUT recipes (repositories touched by the
    # compact engine) are carried over as recipes; only raw RGB objects are packed.
    raw = frames
    if hasattr(old.store, "export_plan"):
        plan = old.store.export_plan([("video", ref) for ref in frames])
        for asset in plan["assets"]:
            if not store.asset_path(asset).is_file():
                with old.store.asset_path(asset).open("rb") as stream:
                    store.import_asset(stream, asset, old.store.asset_path(asset).stat().st_size)
        for item in plan["objects"]:
            if item["kind"] == "lut":
                store.put("lut", old.store.get("lut", item["id"]))
        for video_id, recipe in plan["recipes"].items():
            store.import_recipe(video_id, recipe)
        report["kept_recipes"] = len(plan["recipes"])
        raw = [item["id"] for item in plan["objects"] if item["kind"] == "video"]
        order = {ref: number for number, ref in enumerate(frames)}
        raw.sort(key=lambda ref: order.get(ref, len(order)))  # LUT bases follow timeline order
    covered = set()
    if original:
        covered = _use_original(old, store, partial, Path(original), first, fps, progress)
        report["original_matched"] = bool(covered)
        report["from_original"] = len(covered & set(raw))
    remaining = [ref for ref in raw if ref not in covered]
    if covered and remaining:
        recovered = _recover_luts(old, store, manifests, set(frames) - set(remaining), remaining, progress)
        report["recovered_luts"] = len(recovered)
        remaining = [ref for ref in remaining if ref not in recovered]
    workdir = partial / ".pack"
    workdir.mkdir()
    profile = {"width": width, "height": height, "fps": [fps.numerator, fps.denominator],
               "offset": 0.0, "normalization": 2}
    for start in range(0, len(remaining), clip_frames):
        chunk = remaining[start:start + clip_frames]
        clip = workdir / f"clip-{start // clip_frames:06d}.mkv"
        encode_clip(chunk, width, height, fps, clip, lambda ref: old.store.get("video", ref), preset)
        decoded = decoded_ids(clip, profile, len(chunk))
        exact = len(decoded) == len(chunk) and decoded == chunk
        if exact:
            asset = store.add_asset(clip)
            for index, ref in enumerate(chunk):
                registered = store.register_source_frame(asset, profile, index, old.store.get("video", ref))
                if registered != ref:
                    raise UserError("Хранилище вернуло другой идентификатор кадра при конвертации.")
            report["packed"] += len(chunk)
            report["clips"] += 1
        else:
            # Lossless fallback: keep the legacy RGB object as-is for this clip.
            for ref in chunk:
                store.put("video", old.store.get("video", ref))
            report["fallback"] += len(chunk)
        clip.unlink()
        progress(f"Упаковано кадров: {start + len(chunk)} из {len(remaining)}",
                 .05 + .85 * (start + len(chunk)) / max(1, len(remaining)))
    workdir.rmdir()
    report["width"], report["height"], report["fps"] = width, height, float(fps)
    return report


LUT_TABLES = [bytes(min(255, round(v * factor)) for v in range(256)) for factor in (1.2, .8)]


def _recover_luts(old, store, manifests, known, pending, progress):
    """Re-express V1 brighten/darken frames as LUT recipes over frames already available.

    A raw frame qualifies only if applying the V1 table to the frame at the same
    position of another version reproduces it byte for byte; repeated passes
    cover stacked effects. Anything else is left for lossless packing.
    """
    known, pending, recovered = set(known), set(pending), set()
    progress("Поиск кадров яркости, которые можно описать эффектом…", .06)
    changed = True
    while changed and pending:
        changed = False
        for manifest in manifests.values():
            for index, ref in enumerate(manifest["video"]):
                if ref not in pending:
                    continue
                bases = {other["video"][index] for other in manifests.values()
                         if index < len(other["video"]) and other["video"][index] in known}
                if not bases:
                    continue
                data = old.store.get("video", ref)
                for base in bases:
                    base_data = store.get("video", base)
                    table = next((t for t in LUT_TABLES if base_data.translate(t) == data), None)
                    if table is not None and store.put_video_lut(base, table, data) == ref:
                        known.add(ref)
                        pending.discard(ref)
                        recovered.add(ref)
                        changed = True
                        break
    return recovered


def _use_original(old, store, partial, original, first, fps, progress):
    """Register the original file for the first version only if every frame ID matches."""
    if not original.is_file():
        raise UserError("Исходный видеофайл не найден.")
    progress("Сравнение с исходным видеофайлом…", .02)
    # V1 used the scale/fps pipeline of normalization 1; newer legacy-layout
    # imports may use the bitexact profile 2. Accept whichever reproduces every ID.
    for normalization in (1, 2):
        with tempfile.TemporaryDirectory(dir=partial) as scratch:
            trial = type(store)(Path(scratch))
            try:
                ids = media.decode_frames(trial, original, first["width"], first["height"], fps,
                                          compact=True, normalization=normalization)
            except UserError:
                ids = None
            finally:
                trial.close()
        if ids == first["video"]:
            ids = media.decode_frames(store, original, first["width"], first["height"], fps,
                                      compact=True, normalization=normalization)
            return set(ids)
    return set()


def _verify(old, store, report, progress):
    _, manifest_ids = manifests_to_keep(old)
    checked = set()
    for manifest_id in manifest_ids:
        manifest = old.store.get_json("manifest", manifest_id)
        size = manifest["width"] * manifest["height"] * 3
        for ref in manifest["video"]:
            if ref not in checked:
                # Store.get rebuilds the frame and checks its content ID.
                if len(store.get("video", ref)) != size:
                    raise UserError("Проверка конвертации не прошла: размер кадра не совпал.")
                checked.add(ref)
        for ref in manifest["audio"]:
            if ("a", ref) not in checked:
                store.get("audio", ref)
                checked.add(("a", ref))
    report["verified_frames"] = sum(1 for item in checked if isinstance(item, str))


def format_size(value):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{value} Б"
        value /= 1024


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m vibemogging.maintenance",
                                     description="Обслуживание проектов Vibemogging")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("migrate", help="Скопировать старый проект в новую компактную папку")
    convert.add_argument("source", type=Path, help="Папка старого проекта (не изменяется)")
    convert.add_argument("target", type=Path, help="Новая папка (не должна существовать)")
    convert.add_argument("--source-media", type=Path, help="Исходный видеофайл первой версии (необязательно)")
    convert.add_argument("--clip-frames", type=int, default=CLIP_FRAMES, help="Кадров в одном lossless-клипе")
    args = parser.parse_args(argv)
    if not 1 <= args.clip_frames <= 3000:
        parser.error("--clip-frames: от 1 до 3000")
    try:
        report = migrate(args.source, args.target, args.source_media,
                         lambda text, value: print(f"[{value * 100:5.1f}%] {text}", flush=True),
                         args.clip_frames)
    except UserError as error:
        parser.exit(1, f"Ошибка: {error}\nИсходный проект не изменён.\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Было: {format_size(report['before_bytes'])}, стало: {format_size(report['after_bytes'])}")


if __name__ == "__main__":
    sys.exit(main())
