"""Combined-SRT timing, ffmpeg argument construction, and skip handling."""

from pathlib import Path

from subtitle_clipper import ffmpeg
from subtitle_clipper.clips import (
    ClipRequest,
    CueEntry,
    SegmentPlan,
    _fmt_ts,
    build_combined_srt,
    generate,
)
from subtitle_clipper.search import Match


def _match(media_status, media_path, start=10.0, end=12.5):
    return Match(
        episode_id="show_s01e01_bd", show_slug="show", show_title="Show",
        season=1, episodes=(1,), cue_index=0, start=start, end=end, text="邊個",
        srt_path="A/ep.srt", media_path=media_path, media_status=media_status,
    )


def test_fmt_ts():
    assert _fmt_ts(0.5) == "00:00:00,500"
    assert _fmt_ts(3661.25) == "01:01:01,250"
    assert _fmt_ts(-1) == "00:00:00,000"


def test_build_combined_srt_offsets_and_clamp():
    segments = [
        SegmentPlan(win_start=9.5, win_end=13.0, cues=[CueEntry(10.0, 12.5, "第一")]),
        SegmentPlan(win_start=0.0, win_end=1.5, cues=[CueEntry(0.2, 1.0, "第二")]),
    ]
    text, total = build_combined_srt(segments)
    # clip 1: dur 3.5, cue local 0.5..3.0 on the timeline
    assert "00:00:00,500 --> 00:00:03,000" in text
    # clip 2 starts at offset 3.5; clamped window puts cue at 3.7..4.5
    assert "00:00:03,700 --> 00:00:04,500" in text
    assert total == 5.0
    assert "第一" in text and "第二" in text


def test_build_combined_srt_multiple_cues_share_one_offset():
    segments = [
        SegmentPlan(win_start=0.0, win_end=5.0, cues=[
            CueEntry(1.0, 2.0, "extra"),
            CueEntry(3.0, 4.0, "target"),
        ]),
        SegmentPlan(win_start=0.0, win_end=2.0, cues=[CueEntry(0.5, 1.5, "next")]),
    ]
    text, total = build_combined_srt(segments)
    assert "00:00:01,000 --> 00:00:02,000" in text
    assert "00:00:03,000 --> 00:00:04,000" in text
    # second segment's timeline offset is 5.0 (first segment's duration), not
    # 5.0 + 5.0 -- the offset only advances once per segment, not per cue.
    assert "00:00:05,500 --> 00:00:06,500" in text
    assert total == 7.0


def test_preview_cmd_forces_8bit_yuv420p():
    # 10-bit ("Hi10P") sources decode fine but re-encode to 10-bit H.264, which
    # browsers can't play; the preview must force yuv420p so the <video> works.
    cmd = ffmpeg.preview_cmd(Path("in.mkv"), 1.0, 4.0, Path("out.mp4"), audio_track=0)
    joined = " ".join(cmd)
    assert "format=yuv420p" in joined
    assert "yuv420p" in cmd  # explicit -pix_fmt value too


def test_cut_segment_cmd_shape():
    cmd = ffmpeg.cut_segment_cmd(
        Path("in.mkv"), 9.5, 13.0, Path("out.mkv"),
        audio_track=2, spec=ffmpeg.NormSpec(),
    )
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and "9.500" in cmd
    assert "-t" in cmd and "3.500" in cmd            # duration = end - start
    assert "0:a:2?" in cmd                            # selected + optional
    assert "libx264" in cmd and "aac" in cmd


def test_concat_and_mux_cmd_shape():
    ccmd = ffmpeg.concat_cmd(Path("list.txt"), Path("out.mkv"))
    assert ccmd[:5] == ["ffmpeg", "-nostdin", "-y", "-f", "concat"]
    assert "-safe" in ccmd and "copy" in ccmd

    mcmd = ffmpeg.mux_subtitles_cmd(Path("v.mkv"), Path("s.srt"), Path("o.mkv"))
    assert "-c:s" in mcmd and "srt" in mcmd
    assert "language=yue" in mcmd


def test_burn_subtitles_cmd_shape_and_escaping():
    cmd = ffmpeg.burn_subtitles_cmd(
        Path("v.mkv"), Path("C:\\clips\\combined.srt"), Path("o.mkv"),
        style=ffmpeg.BurnStyle(font="Noto Sans CJK TC", size=32),
    )
    assert cmd[0] == "ffmpeg"
    joined = " ".join(cmd)
    assert "subtitles=" in joined
    assert "C\\:/clips/combined.srt" in joined
    assert "FontName=Noto Sans CJK TC" in joined
    assert "FontSize=32" in joined
    assert "-c:a" in cmd and "copy" in cmd
    assert "-c:s" not in cmd


def test_generate_skips_non_video_without_ffmpeg(tmp_path, monkeypatch):
    # Guard: if any ffmpeg call happens, fail loudly.
    monkeypatch.setattr(ffmpeg, "_run", lambda cmd: (_ for _ in ()).throw(AssertionError("ffmpeg called")))
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    m = _match("audio_only", "a.wav")
    report = generate([ClipRequest(match=m)], tmp_path, tmp_path / "out.mkv")
    assert report.output is None
    assert len(report.skipped) == 1
    assert "audio-only" in report.skipped[0].reason


def test_generate_uses_video_override_even_when_match_has_no_media(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg, "_run", lambda cmd: (_ for _ in ()).throw(AssertionError("ffmpeg called")))
    m = _match("unlinked", None)
    missing = tmp_path / "missing.mkv"  # override points at a file that doesn't exist -> still skipped, but via override path
    report = generate([ClipRequest(match=m, video_override=str(missing))], tmp_path, tmp_path / "out.mkv")
    assert report.output is None
    assert "not found" in report.skipped[0].reason
