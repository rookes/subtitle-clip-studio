"""Turn a set of search matches into one stitched MKV with an embedded SRT.

Pipeline: cut each item's window (padded by default, or an explicit override
set by the user in the UI) to a normalized temp clip, concat them, build a
combined SRT whose timestamps track the stitched timeline, then either mux
that SRT back in as a soft subtitle track or burn it directly into the frame.
Items without a usable video file are skipped and reported, never silently
dropped.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import audio
from .corpus import media_abspath, media_status
from .ffmpeg import BurnStyle, NormSpec, burn_subtitles, concat, cut_segment, mux_subtitles
from .search import Match

DEFAULT_PAD_S = 0.5


@dataclass
class SkippedMatch:
    label: str
    reason: str


@dataclass
class Report:
    output: Path | None
    srt: Path | None
    included: list[Match] = field(default_factory=list)
    skipped: list[SkippedMatch] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Generated {len(self.included)} clip(s)"]
        if self.output:
            lines.append(f"  video: {self.output}")
            lines.append(f"  subs:  {self.srt}")
        for s in self.skipped:
            lines.append(f"  skipped {s.label}: {s.reason}")
        return "\n".join(lines)


# --- combined-SRT math (pure, unit-tested) -----------------------------------

@dataclass(frozen=True)
class CueEntry:
    """One subtitle line's timing (in SOURCE-video seconds) and text."""
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SegmentPlan:
    """One physical cut: its padded window plus every cue line inside it
    (the target line and any auto-detected or manually-added neighbors)."""
    win_start: float
    win_end: float
    cues: list[CueEntry]


@dataclass(frozen=True)
class ClipRequest:
    """One selected result plus the user's session-only overrides for it.

    ``win_start``/``win_end`` of ``None`` fall back to the classic
    ``[match.start - pad_s, match.end + pad_s]`` window (what the CLI, which
    has no per-item timing UI, always uses). ``video_override`` is an
    absolute path string that replaces the corpus-linked media for this item
    only.
    """
    match: Match
    win_start: float | None = None
    win_end: float | None = None
    extra_cues: tuple[CueEntry, ...] = ()
    video_override: str | None = None


def _fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_combined_srt(segments: list[SegmentPlan]) -> tuple[str, float]:
    """Return (srt_text, total_timeline_seconds).

    Each segment becomes one stitched-timeline slot; every cue within it gets
    its own SRT block, offset by that cue's position inside the segment. The
    running timeline offset advances once per segment (by its clip duration),
    not once per cue, so multiple lines in one segment share the same base.
    """
    blocks: list[str] = []
    offset = 0.0
    n = 0
    for seg in segments:
        clip_dur = max(0.0, seg.win_end - seg.win_start)
        for cue in seg.cues:
            local_start = max(0.0, cue.start - seg.win_start)
            local_end = min(clip_dur, cue.end - seg.win_start)
            if local_end <= local_start:
                local_end = clip_dur
            n += 1
            start_ts = _fmt_ts(offset + local_start)
            end_ts = _fmt_ts(offset + local_end)
            blocks.append(f"{n}\n{start_ts} --> {end_ts}\n{cue.text}\n")
        offset += clip_dur
    return "\n".join(blocks), offset


# --- audio-track selection ---------------------------------------------------

def _select_audio_track(src: Path) -> int:
    """Pick the preferred audio stream (Cantonese by default; configurable via
    the ``[audio]`` table in corpus.toml — see :mod:`subtitle_clipper.audio`)."""
    return audio.select_audio_track(src)


# --- generation --------------------------------------------------------------

def generate(
    items: list[ClipRequest],
    media_root: Path | None,
    out_path: Path,
    *,
    pad_s: float = DEFAULT_PAD_S,
    spec: NormSpec | None = None,
    records_by_id: dict | None = None,
    burn_in: BurnStyle | None = None,
) -> Report:
    """Cut, stitch and subtitle the given items into ``out_path`` (MKV).

    ``records_by_id`` maps episode_id -> EpisodeRecord; matches carry a
    media_path but resolving to an absolute file needs the record's path plus
    ``media_root``. When omitted, ``match.media_path`` is joined to media_root
    directly. ``burn_in``, when set, hardcodes the subtitles into the frame
    instead of muxing them as a soft track.
    """
    spec = spec or NormSpec()
    out_path = Path(out_path)
    report = Report(output=None, srt=None)

    plan: list[tuple[ClipRequest, Path]] = []
    for item in items:
        m = item.match
        src = _resolve_src(item, media_root, records_by_id)
        status = m.media_status if src is None else media_status_for(src)
        if status != "video":
            report.skipped.append(SkippedMatch(_label(m), _skip_reason(status)))
            continue
        plan.append((item, src))

    if not plan:
        return report

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clipper_") as tmp:
        work = Path(tmp)
        segment_files: list[Path] = []
        segment_plans: list[SegmentPlan] = []
        for idx, (item, src) in enumerate(plan):
            m = item.match
            win_start = item.win_start if item.win_start is not None else max(0.0, m.start - pad_s)
            win_end = item.win_end if item.win_end is not None else m.end + pad_s
            seg_file = work / f"seg{idx:04d}.mkv"
            cut_segment(src, win_start, win_end, seg_file,
                        audio_track=_select_audio_track(src), spec=spec)
            segment_files.append(seg_file)
            cues = sorted(
                [CueEntry(m.start, m.end, m.text), *item.extra_cues],
                key=lambda c: c.start,
            )
            segment_plans.append(SegmentPlan(win_start, win_end, cues))
            report.included.append(m)

        srt_text, _ = build_combined_srt(segment_plans)
        combined_srt = work / "combined.srt"
        combined_srt.write_text(srt_text, encoding="utf-8")

        stitched = work / "stitched.mkv"
        concat(segment_files, stitched, work_dir=work)
        if burn_in is not None:
            burn_subtitles(stitched, combined_srt, out_path, style=burn_in, crf=spec.crf)
        else:
            mux_subtitles(stitched, combined_srt, out_path,
                          language=audio.subtitle_language_tag())

    sidecar = out_path.with_suffix(".srt")
    sidecar.write_text(srt_text, encoding="utf-8")
    report.output = out_path
    report.srt = sidecar
    return report


def media_status_for(src: Path) -> str:
    from .corpus import AUDIO_ONLY_EXTENSIONS, VIDEO_EXTENSIONS
    if not src.is_file():
        return "missing"
    suf = src.suffix.lower()
    if suf in VIDEO_EXTENSIONS:
        return "video"
    if suf in AUDIO_ONLY_EXTENSIONS:
        return "audio_only"
    return "unknown"


def _resolve_src(item: ClipRequest, media_root: Path | None, records_by_id: dict | None) -> Path | None:
    if item.video_override:
        return Path(item.video_override)
    m = item.match
    if records_by_id and m.episode_id in records_by_id:
        return media_abspath(records_by_id[m.episode_id], media_root)
    if m.media_path is None or media_root is None:
        return None
    return media_root / m.media_path


def _label(m: Match) -> str:
    return f"{m.show_title} [{m.episode_id}] @ {_fmt_ts(m.start)}"


def _skip_reason(status: str) -> str:
    return {
        "audio_only": "linked media is audio-only (no video track)",
        "missing": "linked media file not found on disk",
        "unlinked": "no media linked (set a media root that mirrors the subtitle tree)",
        "unknown": "linked media has an unrecognized format",
    }.get(status, f"unusable media ({status})")
