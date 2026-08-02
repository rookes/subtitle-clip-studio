"""Turn a set of search matches into one stitched clip with its subtitles.

Pipeline: cut each item's window (padded by default, or an explicit override
set by the user in the UI) to a normalized temp clip, concat them, build a
combined SRT whose timestamps track the stitched timeline, then either mux
that SRT back in as a soft subtitle track or burn it directly into the frame.
Items without a usable video file are skipped and reported, never silently
dropped.

The output container comes from ``out_path``'s suffix (``.mp4`` by default,
``.mkv`` also supported); :mod:`subtitle_clipper.ffmpeg` derives the muxer flags
and subtitle codec from it.
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
    output: Path | None                              # first (or only) video
    srt: Path | None                                 # its sidecar, if any
    included: list[Match] = field(default_factory=list)
    skipped: list[SkippedMatch] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)  # every video written
    # Matching sidecars, positionally aligned with ``outputs``. Entries are
    # None when the subtitles were burned into the frame — an external file
    # would just be a duplicate of what's already on screen.
    srts: list[Path | None] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Generated {len(self.included)} clip(s)"]
        for out, srt in zip(self.outputs, self.srts):
            lines.append(f"  video: {out}")
            if srt is not None:
                lines.append(f"  subs:  {srt}")
        for s in self.skipped:
            lines.append(f"  skipped {s.label}: {s.reason}")
        return "\n".join(lines)


# --- combined-SRT math (pure, unit-tested) -----------------------------------

@dataclass(frozen=True)
class CueEntry:
    """One subtitle line's timing (in SOURCE-video seconds) and text.

    ``highlights`` are half-open ``[start, end)`` character offsets into
    ``text`` that the user emphasised in the Edit Timing panel. They are kept
    as offsets rather than inline markup so ``text`` stays the plain on-screen
    string everywhere; the markup is generated once, at SRT serialization.
    """
    start: float
    end: float
    text: str
    highlights: tuple[tuple[int, int], ...] = ()


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
    # Highlight ranges for the match's own line (extras carry their own).
    target_highlights: tuple[tuple[int, int], ...] = ()


# Highlighted text is emitted as a SubRip font tag. ffmpeg's SubRip decoder
# turns it into an inline ASS colour override, so the same markup drives both
# the downloadable .srt and the libass burn-in.
HIGHLIGHT_COLOR = "#fff000"


def normalize_highlights(
    ranges, text_len: int
) -> tuple[tuple[int, int], ...]:
    """Clamp, drop and merge highlight ranges into canonical sorted form.

    Offsets arrive from the browser, so nothing here trusts them: out-of-bounds
    values are clamped, empty/inverted ranges dropped, and overlapping or
    touching ranges merged so serialization can walk them in one pass.
    """
    clean: list[tuple[int, int]] = []
    for r in ranges or ():
        try:
            start, end = int(r[0]), int(r[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        start = max(0, min(start, text_len))
        end = max(0, min(end, text_len))
        if end > start:
            clean.append((start, end))
    clean.sort()

    merged: list[tuple[int, int]] = []
    for start, end in clean:
        if merged and start <= merged[-1][1]:      # overlapping or adjacent
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def apply_highlights(text: str, ranges) -> str:
    """Wrap each highlighted range of ``text`` in a yellow ``<font>`` tag.

    Wrappers are closed and reopened at line breaks so every SRT line carries
    balanced markup — a tag straddling a newline is not reliably handled by
    players or by ffmpeg's SubRip decoder.
    """
    spans = normalize_highlights(ranges, len(text))
    if not spans:
        return text

    open_tag = f'<font color="{HIGHLIGHT_COLOR}">'
    lines: list[str] = []
    base = 0
    for line in text.split("\n"):
        stop = base + len(line)
        cursor = base
        parts: list[str] = []
        for start, end in spans:
            start, end = max(start, base), min(end, stop)
            if end <= start:
                continue                            # span is on another line
            parts.append(text[cursor:start])
            parts.append(open_tag + text[start:end] + "</font>")
            cursor = end
        parts.append(text[cursor:stop])
        lines.append("".join(parts))
        base = stop + 1                             # step over the "\n"
    return "\n".join(lines)


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
            body = apply_highlights(cue.text, cue.highlights)
            blocks.append(f"{n}\n{start_ts} --> {end_ts}\n{body}\n")
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
    separate: bool = False,
) -> Report:
    """Cut, stitch and subtitle the given items into ``out_path``.

    ``records_by_id`` maps episode_id -> EpisodeRecord; matches carry a
    media_path but resolving to an absolute file needs the record's path plus
    ``media_root``. When omitted, ``match.media_path`` is joined to media_root
    directly. ``burn_in``, when set, hardcodes the subtitles into the frame
    instead of muxing them as a soft track.

    ``separate`` writes one video (+ ``.srt`` sidecar, unless burned in) per
    clip — named ``{stem}-{n:02d}{suffix}`` beside ``out_path`` — instead of
    concatenating every clip into the single ``out_path``.
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
        segments: list[tuple[SegmentPlan, Path]] = []
        for idx, (item, src) in enumerate(plan):
            m = item.match
            win_start = item.win_start if item.win_start is not None else max(0.0, m.start - pad_s)
            # A merged bookmark run spans several lines, so the default window
            # pads the whole entry, not just its first line.
            entry_end = m.group_end if m.group_end is not None else m.end
            win_end = item.win_end if item.win_end is not None else entry_end + pad_s
            seg_file = work / f"seg{idx:04d}.mkv"
            cut_segment(src, win_start, win_end, seg_file,
                        audio_track=_select_audio_track(src), spec=spec)
            cues = sorted(
                [CueEntry(m.start, m.end, m.text, item.target_highlights), *item.extra_cues],
                key=lambda c: c.start,
            )
            segments.append((SegmentPlan(win_start, win_end, cues), seg_file))
            report.included.append(m)

        if separate:
            width = max(2, len(str(len(segments))))
            for n, (seg_plan, seg_file) in enumerate(segments, start=1):
                dest = out_path.with_name(f"{out_path.stem}-{n:0{width}d}{out_path.suffix}")
                out, sidecar = _finalize([seg_file], [seg_plan], dest, work, burn_in, spec)
                report.outputs.append(out)
                report.srts.append(sidecar)
        else:
            out, sidecar = _finalize([f for _, f in segments], [p for p, _ in segments],
                                     out_path, work, burn_in, spec)
            report.outputs.append(out)
            report.srts.append(sidecar)

    if report.outputs:
        report.output = report.outputs[0]
        report.srt = report.srts[0]
    return report


def _finalize(segment_files: list[Path], segment_plans: list[SegmentPlan],
              out_path: Path, work: Path, burn_in: BurnStyle | None,
              spec: NormSpec) -> tuple[Path, Path | None]:
    """Concat the given segments, build their combined SRT and mux/burn it in.

    Returns ``(video, srt)``. The ``.srt`` sidecar is only written when the
    subtitles were muxed as a soft track — when they're burned into the frame
    the text is already on screen, so an external file is redundant and ``srt``
    is ``None``.
    """
    srt_text, _ = build_combined_srt(segment_plans)
    combined_srt = work / (out_path.stem + ".srt")
    combined_srt.write_text(srt_text, encoding="utf-8")

    stitched = work / (out_path.stem + ".stitched.mkv")
    concat(segment_files, stitched, work_dir=work)
    if burn_in is not None:
        burn_subtitles(stitched, combined_srt, out_path, style=burn_in, crf=spec.crf)
        return out_path, None

    mux_subtitles(stitched, combined_srt, out_path,
                  language=audio.subtitle_language_tag())
    sidecar = out_path.with_suffix(".srt")
    sidecar.write_text(srt_text, encoding="utf-8")
    return out_path, sidecar


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
