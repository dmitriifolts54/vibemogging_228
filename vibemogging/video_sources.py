"""Bounded, lazy reconstruction of frames from immutable compressed sources."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import subprocess
import tempfile

from .storage import UserError


def video_filters(width, height, fps, normalization=2):
    flags = ":flags=bilinear+accurate_rnd+bitexact" if normalization == 2 else ""
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease{flags},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps.numerator}/{fps.denominator}:start_time=0,setsar=1")


def video_command(path: Path, profile: dict, *, fast_frame=None, count=None):
    fps = Fraction(*profile["fps"])
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-threads", "2", "-filter_threads", "1"]
    if profile["normalization"] == 2:
        command += ["-cpuflags", "0", "-fflags", "+bitexact", "-flags", "+bitexact"]
    if fast_frame is not None:
        command += ["-ss", f"{profile['offset'] + fast_frame / float(fps):.12f}"]
    command += ["-i", str(path)]
    if fast_frame is None:
        command += ["-ss", f"{profile['offset']:.12f}"]
    command += ["-map", "0:v:0", "-an", "-vf",
                video_filters(profile["width"], profile["height"], fps, profile["normalization"]),
                "-pix_fmt", "rgb24"]
    if count is not None:
        command += ["-frames:v", str(count)]
    command += ["-f", "rawvideo", "pipe:1"]
    return command


class FrameDecoder:
    def __init__(self, path: Path, profile: dict):
        self.path, self.profile = path, profile
        self.process = None
        self.errors = None
        self.next_frame = 0
        self.fast = False

    def start(self, index, *, accurate=False):
        self.close()
        self.fast = bool(index and not accurate)
        self.next_frame = index if self.fast else 0
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            video_command(self.path, self.profile, fast_frame=index if self.fast else None),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self.errors)

    def read(self, index, *, accurate=False):
        maximum_skip = max(8, round(self.profile["fps"][0] / self.profile["fps"][1] * 2))
        if (not self.process or index < self.next_frame or
            (index - self.next_frame > maximum_skip and not accurate)):
            self.start(index, accurate=accurate)
        size = self.profile["width"] * self.profile["height"] * 3
        result = b""
        while self.next_frame <= index:
            buffer = bytearray()
            while len(buffer) < size:
                part = self.process.stdout.read(size - len(buffer))
                if not part:
                    self.close()
                    raise UserError("Не удалось восстановить кадр из исходного видео.")
                buffer.extend(part)
            result = bytes(buffer)
            self.next_frame += 1
        return result

    def close(self):
        if self.process:
            if self.process.stdout:
                self.process.stdout.close()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            else:
                self.process.wait()
            self.process = None
        if self.errors:
            self.errors.close()
            self.errors = None
