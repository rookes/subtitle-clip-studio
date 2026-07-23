"""Thin, testable ffmpeg/ffprobe subprocess wrappers.

Each public function builds an argument list (exposed as ``*_cmd`` so tests can
assert on it without spawning ffmpeg) and runs it. Covers video cutting, concat,
subtitle muxing / burn-in, and browser-playable previews.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Target normalization for concat: heterogeneous sources (varying codec /
# resolution / fps / sample rate) must be re-encoded to identical parameters
# before the concat demuxer can join them with a stream copy.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 25
DEFAULT_SAMPLE_RATE = 48000


class FFmpegError(RuntimeError):
    pass


DEFAULT_CRF = 20


@dataclass(frozen=True)
class NormSpec:
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    sample_rate: int = DEFAULT_SAMPLE_RATE
    crf: int = DEFAULT_CRF          # libx264 quality (lower = better/larger)

    def vfilter(self) -> str:
        return (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={self.fps},format=yuv420p"
        )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found; ensure ffmpeg is installed and on PATH") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-8:]
        raise FFmpegError(
            f"ffmpeg failed ({proc.returncode}) for {cmd[0]}:\n" + "\n".join(tail)
        )


# --- command builders --------------------------------------------------------

def cut_segment_cmd(
    src: Path, start: float, end: float, out: Path, *, audio_track: int, spec: NormSpec
) -> list[str]:
    """Re-encode ``[start, end)`` of ``src`` to a normalized clip at ``out``."""
    duration = max(0.0, end - start)
    return [
        "ffmpeg", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", f"0:a:{audio_track}?",
        "-vf", spec.vfilter(),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(spec.crf),
        "-c:a", "aac", "-ar", str(spec.sample_rate), "-ac", "2", "-b:a", "192k",
        "-video_track_timescale", "90000",
        str(out),
    ]


def concat_cmd(list_file: Path, out: Path) -> list[str]:
    """Concat normalized segments (identical params) via stream copy."""
    return [
        "ffmpeg", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(out),
    ]


@dataclass(frozen=True)
class BurnStyle:
    """libass ``force_style`` knobs for hardcoding subs into the frame.

    Colors are ASS/libass ``&HAABBGGRR`` hex (alpha-blue-green-red, not RGB).
    """
    font: str = "Arial"
    size: int = 28
    primary_color: str = "&H00FFFFFF"   # opaque white
    outline_color: str = "&H00000000"   # opaque black
    outline: int = 2


def _escape_for_filter(path: Path) -> str:
    """Escape a path for embedding in an ffmpeg filtergraph string.

    The ``subtitles=`` filter parses its argument like a mini command line:
    ``:`` separates key=value options and ``\\`` is the escape character, so a
    Windows drive letter (``C:``) must become ``C\\:`` and backslashes must be
    forward-slashed first so they aren't mistaken for escapes themselves.
    """
    return str(path).replace("\\", "/").replace(":", r"\:")


def burn_subtitles_cmd(video: Path, srt: Path, out: Path, *, style: BurnStyle,
                       crf: int = DEFAULT_CRF) -> list[str]:
    """Re-encode ``video`` with ``srt`` rendered directly into the frame."""
    force_style = (
        f"FontName={style.font},FontSize={style.size},"
        f"PrimaryColour={style.primary_color},OutlineColour={style.outline_color},"
        f"Outline={style.outline},BorderStyle=1"
    )
    vf = f"subtitles='{_escape_for_filter(srt)}':force_style='{force_style}'"
    return [
        "ffmpeg", "-nostdin", "-y",
        "-i", str(video),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-c:a", "copy",
        str(out),
    ]


def mux_subtitles_cmd(video: Path, srt: Path, out: Path, *, language: str = "yue") -> list[str]:
    """Embed ``srt`` as a soft subtitle track in ``video`` (MKV, stream copy)."""
    return [
        "ffmpeg", "-nostdin", "-y",
        "-i", str(video), "-i", str(srt),
        "-map", "0", "-map", "1",
        "-c", "copy", "-c:s", "srt",
        "-metadata:s:s:0", f"language={language}",
        "-disposition:s:0", "default",
        str(out),
    ]


def preview_cmd(
    src: Path, start: float, end: float, out: Path, *, audio_track: int, height: int = 480
) -> list[str]:
    """A small, browser-playable H.264/MP4 of the window for the web preview."""
    duration = max(0.0, end - start)
    return [
        "ffmpeg", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", f"0:a:{audio_track}?",
        # format=yuv420p forces 8-bit output: many sources (esp. 10-bit "Hi10P"
        # anime releases) decode fine but re-encode to 10-bit H.264, which
        # browsers can't play — the clip looks "corrupt" in the <video> tag even
        # though generation (which normalizes to yuv420p) works.
        "-vf", f"scale=-2:{height},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]


# --- runners -----------------------------------------------------------------

def cut_segment(src: Path, start: float, end: float, out: Path, *, audio_track: int, spec: NormSpec) -> None:
    _run(cut_segment_cmd(src, start, end, out, audio_track=audio_track, spec=spec))


def concat(segments: list[Path], out: Path, *, work_dir: Path) -> None:
    list_file = work_dir / "concat_list.txt"
    # concat demuxer paths: single-quote and escape embedded quotes.
    lines = ["file '" + str(p).replace("'", "'\\''") + "'" for p in segments]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(concat_cmd(list_file, out))


def mux_subtitles(video: Path, srt: Path, out: Path, *, language: str = "yue") -> None:
    _run(mux_subtitles_cmd(video, srt, out, language=language))


def burn_subtitles(video: Path, srt: Path, out: Path, *, style: BurnStyle,
                   crf: int = DEFAULT_CRF) -> None:
    _run(burn_subtitles_cmd(video, srt, out, style=style, crf=crf))


def preview(src: Path, start: float, end: float, out: Path, *, audio_track: int, height: int = 480) -> None:
    _run(preview_cmd(src, start, end, out, audio_track=audio_track, height=height))
