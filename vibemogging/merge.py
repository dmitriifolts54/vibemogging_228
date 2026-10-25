"""Three-way media merge. Pixel fusion is deliberately a replaceable hook."""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from .storage import Store, UserError

AUDIO_BLOCK = 4096  # stereo sample frames; unrelated to video frame boundaries
CHANNELS = 2
SAMPLE_RATE = 48000
SAMPLE_BYTES = 2
FRAME_BYTES = CHANNELS * SAMPLE_BYTES
PixelResolver = Callable[[bytes, bytes, bytes, dict], bytes | None]


def read_audio(store: Store, manifest: dict, start_byte: int, end_byte: int) -> bytes:
    if end_byte <= start_byte:
        return b""
    size = AUDIO_BLOCK * FRAME_BYTES
    output = bytearray()
    for index in range(start_byte // size, (end_byte - 1) // size + 1):
        data = store.get("audio", manifest["audio"][index])
        begin = max(0, start_byte - index * size)
        end = min(len(data), end_byte - index * size)
        output.extend(data[begin:end])
    return bytes(output)


def write_audio(store: Store, manifest: dict, start_byte: int, data: bytes):
    if not data:
        return
    size = AUDIO_BLOCK * FRAME_BYTES
    end_byte = start_byte + len(data)
    if start_byte < 0 or end_byte > manifest["samples"] * FRAME_BYTES:
        raise UserError("Аудиофрагмент выходит за границы ролика.")
    for index in range(start_byte // size, (end_byte - 1) // size + 1):
        block = bytearray(store.get("audio", manifest["audio"][index]))
        begin = max(start_byte, index * size)
        end = min(end_byte, index * size + len(block))
        block[begin - index * size:end - index * size] = data[begin - start_byte:end - start_byte]
        manifest["audio"][index] = store.put("audio", bytes(block))


def ranges(indices):
    """Coalesce sorted integer positions into half-open ranges."""
    start = previous = None
    for value in indices:
        if start is None:
            start = previous = value
        elif value == previous + 1:
            previous = value
        else:
            yield start, previous + 1
            start = previous = value
    if start is not None:
        yield start, previous + 1


def compatible(*manifests):
    keys = ("width", "height", "fps", "samples")
    first = manifests[0]
    if any(any(m[key] != first[key] for key in keys) or len(m["video"]) != len(first["video"])
           for m in manifests[1:]):
        raise UserError("V1 объединяет только версии с одинаковой длительностью, FPS и разрешением.")


def merge_manifests(store: Store, base: dict, ours: dict, theirs: dict,
                    pixel_resolver: PixelResolver | None = None):
    compatible(base, ours, theirs)
    result = deepcopy(ours)
    video_conflicts = []
    audio_conflicts = []
    for index, (b, a, c) in enumerate(zip(base["video"], ours["video"], theirs["video"])):
        if a == c or c == b:
            continue
        if a == b:
            result["video"][index] = c
            continue
        if pixel_resolver:
            fused = pixel_resolver(store.get("video", b), store.get("video", a),
                                   store.get("video", c), {"frame": index, "width": base["width"],
                                                           "height": base["height"]})
            if fused is not None:
                if len(fused) != base["width"] * base["height"] * 3:
                    raise UserError("Функция слияния вернула кадр неверного размера.")
                result["video"][index] = store.put("video", fused)
                continue
        video_conflicts.append(index)
    for index, (b, a, c) in enumerate(zip(base["audio"], ours["audio"], theirs["audio"])):
        if a == c or c == b:
            continue
        if a == b:
            result["audio"][index] = c
            continue
        original, left, right = (store.get("audio", ref) for ref in (b, a, c))
        merged = bytearray(left)
        # Compare individual PCM channel samples, not entire storage blocks.
        for offset in range(0, len(left), SAMPLE_BYTES):
            bv, av, cv = (data[offset:offset + SAMPLE_BYTES] for data in (original, left, right))
            if av == cv or cv == bv:
                continue
            if av == bv:
                merged[offset:offset + SAMPLE_BYTES] = cv
            else:
                audio_conflicts.append(index * AUDIO_BLOCK * CHANNELS + offset // SAMPLE_BYTES)
        result["audio"][index] = store.put("audio", bytes(merged))
    conflicts = []
    for kind, positions in (("video", video_conflicts), ("audio", audio_conflicts)):
        for start, end in ranges(positions):
            conflicts.append({"id": f"{kind}:{start}:{end}", "kind": kind,
                              "start": start, "end": end, "choice": None})
    return result, conflicts


def diff_manifests(store: Store, left: dict, right: dict):
    compatible(left, right)
    fps = left["fps"][0] / left["fps"][1]
    video = list(ranges(i for i, (a, b) in enumerate(zip(left["video"], right["video"])) if a != b))

    def changed_samples():
        for index, (a, b) in enumerate(zip(left["audio"], right["audio"])):
            if a == b:
                continue
            av, bv = store.get("audio", a), store.get("audio", b)
            for offset in range(0, len(av), FRAME_BYTES):
                if av[offset:offset + FRAME_BYTES] != bv[offset:offset + FRAME_BYTES]:
                    yield index * AUDIO_BLOCK + offset // FRAME_BYTES

    audio = list(ranges(changed_samples()))
    return {"video": [{"start": a / fps, "end": b / fps, "first": a, "last": b} for a, b in video],
            "audio": [{"start": a / SAMPLE_RATE, "end": b / SAMPLE_RATE} for a, b in audio],
            "video_frames": sum(b - a for a, b in video),
            "audio_samples": sum(b - a for a, b in audio)}
