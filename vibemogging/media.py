"""Streaming FFmpeg adapters. Repository media is always lossless RGB/PCM."""

from __future__ import annotations

from array import array
from copy import deepcopy
from fractions import Fraction
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import zlib

from .merge import AUDIO_BLOCK, CHANNELS, FRAME_BYTES, SAMPLE_RATE, read_audio, write_audio
from .storage import Store, UserError


def tools_available():
    return all(shutil.which(name) for name in ("ffmpeg", "ffprobe"))


def probe(path: Path):
    if not tools_available():
        raise UserError("Установите FFmpeg и ffprobe: sudo apt install ffmpeg")
    try:
        process = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                  "-of", "json", str(path)], capture_output=True, timeout=60)
        if process.returncode:
            raise UserError("Не удалось открыть медиафайл. Проверьте его формат.")
        return json.loads(process.stdout)
    except subprocess.TimeoutExpired as error:
        raise UserError("Превышено время чтения медиафайла.") from error


def command(*args):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "2", "-filter_threads", "1", *map(str, args)]


def read_exact(stream, count):
    output = bytearray()
    while len(output) < count:
        chunk = stream.read(count - len(output))
        if not chunk:
            break
        output.extend(chunk)
    return bytes(output)


def decode_frames(store: Store, path: Path, width: int, height: int, fps: Fraction,
                  offset=0, count=None, progress=lambda value: None):
    filters = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps.numerator}/{fps.denominator}:start_time=0,"
               "setsar=1")
    # Output seeking decodes from the beginning. Input seeking can skip early
    # audio when the first video keyframe is delayed relative to the soundtrack.
    args = command("-i", path, "-ss", offset, "-map", "0:v:0", "-an", "-vf", filters,
                   "-pix_fmt", "rgb24")
    if count is not None:
        args += ["-frames:v", str(count)]
    args += ["-f", "rawvideo", "pipe:1"]
    frames = []
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
        try:
            while True:
                frame = read_exact(process.stdout, width * height * 3)
                if not frame:
                    break
                if len(frame) != width * height * 3:
                    raise UserError("FFmpeg вернул неполный кадр.")
                frames.append(store.put("video", frame))
                progress(len(frames))
            if process.wait() != 0:
                errors.seek(0)
                raise UserError("Не удалось декодировать видео: " + errors.read(1000).decode(errors="replace"))
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    if count is not None and len(frames) != count:
        raise UserError("Видео для замены короче выбранного диапазона. Выберите более длинный фрагмент.")
    if not frames:
        raise UserError("В файле не найдено видеокадров.")
    return frames


def decode_audio(path: Path, target: Path, duration: float, offset=0, required=False):
    streams = probe(path)["streams"]
    if not any(s["codec_type"] == "audio" for s in streams):
        if required:
            raise UserError("В выбранном файле нет звука.")
        target.write_bytes(b"")
        return
    process = subprocess.run(command("-i", path, "-ss", offset, "-map", "0:a:0", "-vn",
                                      "-af", "aresample=48000:async=1:first_pts=0",
                                      "-t", f"{duration:.12f}", "-ar", SAMPLE_RATE,
                                      "-ac", CHANNELS, "-acodec", "pcm_s16le", "-f", "s16le", target),
                             capture_output=True)
    if process.returncode:
        raise UserError("Не удалось декодировать звук: " + process.stderr[:1000].decode(errors="replace"))


def import_media(store: Store, path: Path, progress=lambda value: None):
    metadata = probe(path)
    video = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise UserError("Для создания проекта нужен файл с видео.")
    width, height = int(video["width"]), int(video["height"])
    rotation = next((int(d.get("rotation", 0)) for d in video.get("side_data_list", [])
                     if "rotation" in d), 0)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    width += width % 2
    height += height % 2
    if width * height > 3840 * 2160:
        raise UserError("Первая версия поддерживает разрешение до 3840 × 2160.")
    try:
        fps = Fraction(video.get("avg_frame_rate", "0/1"))
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0)
    if not 0 < fps <= 240:
        fps = Fraction(30)
    estimate = float(metadata.get("format", {}).get("duration", 1)) * float(fps)
    frames = decode_frames(store, path, width, height, fps,
                           progress=lambda n: progress(min(.8, n / max(estimate, 1) * .8)))
    sample_count = round(Fraction(len(frames), 1) / fps * SAMPLE_RATE)
    chunks = []
    with tempfile.TemporaryDirectory() as directory:
        audio = Path(directory) / "audio.pcm"
        decode_audio(path, audio, sample_count / SAMPLE_RATE)
        with audio.open("rb") as stream:
            for start in range(0, sample_count, AUDIO_BLOCK):
                length = min(AUDIO_BLOCK, sample_count - start) * FRAME_BYTES
                block = stream.read(length).ljust(length, b"\0")
                chunks.append(store.put("audio", block))
                progress(.8 + .19 * (start + length / FRAME_BYTES) / sample_count)
    return {"width": width, "height": height, "fps": [fps.numerator, fps.denominator],
            "video": frames, "audio": chunks, "samples": sample_count}


def interval(manifest: dict, start, end, video=False):
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError) as error:
        raise UserError("Введите начало и конец диапазона в секундах.") from error
    fps = Fraction(*manifest["fps"])
    duration = len(manifest["video"]) / float(fps)
    if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end <= start or end > duration + 1e-6:
        raise UserError("Диапазон должен находиться внутри ролика; конец — позже начала.")
    if video:
        first, last = round(start * float(fps)), round(end * float(fps))
        last = min(last, len(manifest["video"]))
    else:
        first, last = round(start * SAMPLE_RATE), min(round(end * SAMPLE_RATE), manifest["samples"])
    if first >= last:
        raise UserError("Выбранный диапазон слишком короткий.")
    return first, last


def edit_media(store: Store, manifest: dict, operation: str, start, end, source: Path | None = None,
               offset=0, value=None, progress=lambda value: None):
    result = deepcopy(manifest)
    if not isinstance(operation, str) or operation not in {
            "replace_video", "replace_audio", "brighten", "darken", "fill", "mute", "gain"}:
        raise UserError("Неизвестная операция редактирования.")
    try:
        offset = float(offset)
    except (TypeError, ValueError) as error:
        raise UserError("Некорректное смещение источника.") from error
    if not math.isfinite(offset) or offset < 0:
        raise UserError("Смещение источника должно быть неотрицательным.")
    is_video = operation in {"replace_video", "brighten", "darken", "fill"}
    first, last = interval(manifest, start, end, is_video)
    if operation == "replace_video":
        if source is None:
            raise UserError("Выберите файл для замены.")
        result["video"][first:last] = decode_frames(store, source, manifest["width"], manifest["height"],
                                                   Fraction(*manifest["fps"]), offset, last - first,
                                                   lambda n: progress(n / (last - first)))
    elif operation in {"brighten", "darken", "fill"}:
        if operation == "fill":
            try:
                color = bytes.fromhex(str(value).lstrip("#"))
                if len(color) != 3:
                    raise ValueError
            except ValueError as error:
                raise UserError("Укажите цвет в формате #RRGGBB.") from error
            frame = color * (manifest["width"] * manifest["height"])
            ref = store.put("video", frame)
            result["video"][first:last] = [ref] * (last - first)
        else:
            factor = 1.2 if operation == "brighten" else .8
            table = bytes(min(255, round(v * factor)) for v in range(256))
            for i in range(first, last):
                result["video"][i] = store.put("video", store.get("video", manifest["video"][i]).translate(table))
                progress((i - first + 1) / (last - first))
    elif operation == "replace_audio":
        if source is None:
            raise UserError("Выберите файл для замены звука.")
        required_bytes = (last - first) * FRAME_BYTES
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replacement.pcm"
            decode_audio(source, path, (last - first) / SAMPLE_RATE, offset, required=True)
            if path.stat().st_size < required_bytes:
                raise UserError("Звуковой файл короче выбранного диапазона.")
            with path.open("rb") as stream:
                for begin in range(first, last, AUDIO_BLOCK):
                    count = min(AUDIO_BLOCK, last - begin) * FRAME_BYTES
                    write_audio(store, result, begin * FRAME_BYTES, stream.read(count))
                    progress((begin - first + count / FRAME_BYTES) / (last - first))
    else:
        try:
            gain = 0.0 if operation == "mute" else float(value)
        except (TypeError, ValueError) as error:
            raise UserError("Укажите коэффициент громкости от 0 до 4.") from error
        if not math.isfinite(gain) or not 0 <= gain <= 4:
            raise UserError("Коэффициент громкости должен быть от 0 до 4.")
        for begin in range(first, last, AUDIO_BLOCK):
            finish = min(last, begin + AUDIO_BLOCK)
            raw = read_audio(store, result, begin * FRAME_BYTES, finish * FRAME_BYTES)
            values = array("h")
            values.frombytes(raw)
            if sys.byteorder != "little":
                values.byteswap()
            changed = array("h", (max(-32768, min(32767, round(v * gain))) for v in values))
            if sys.byteorder != "little":
                changed.byteswap()
            write_audio(store, result, begin * FRAME_BYTES, changed.tobytes())
            progress((finish - first) / (last - first))
    return result


def render(store: Store, manifest: dict, output: Path, progress=lambda value: None):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        audio_path = Path(directory) / "audio.wav"
        target = Path(directory) / "render.mp4"
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(CHANNELS)
            audio.setsampwidth(2)
            audio.setframerate(SAMPLE_RATE)
            for ref in manifest["audio"]:
                audio.writeframesraw(store.get("audio", ref))
        width, height = manifest["width"], manifest["height"]
        fps = Fraction(*manifest["fps"])
        args = command("-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
                       "-framerate", f"{fps.numerator}/{fps.denominator}", "-i", "pipe:0",
                       "-i", audio_path, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                       "-threads", "2", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", target)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                for index, ref in enumerate(manifest["video"]):
                    process.stdin.write(store.get("video", ref))
                    progress((index + 1) / len(manifest["video"]))
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
                raise UserError("Ошибка экспорта: " + errors.read(1500).decode(errors="replace"))
        target.replace(output)


def png_frame(store: Store, manifest: dict, index: int, other: dict | None = None):
    if not 0 <= index < len(manifest["video"]):
        raise UserError("Кадр за пределами ролика.")
    raw = store.get("video", manifest["video"][index])
    if other:
        original = store.get("video", other["video"][index])
        raw = bytes(abs(a - b) for a, b in zip(raw, original))
    width, height = manifest["width"], manifest["height"]
    scanlines = b"".join(b"\0" + raw[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines, 3)) + chunk(b"IEND", b""))
