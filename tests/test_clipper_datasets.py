"""Custom datasets (single SRT / directory / bookmarks), empty-query search,
cue_id restriction, and per-clip (separate) generation."""

import json
from pathlib import Path

import pytest

from subtitle_clipper import clips
from subtitle_clipper.clips import ClipRequest, CueEntry
from subtitle_clipper.corpus import Corpus
from subtitle_clipper.datasets import (
    DatasetError,
    group_bookmark_indices,
    load_bookmarks,
    load_directory,
    load_single_srt,
)
from subtitle_clipper.manifest import EpisodeRecord
from subtitle_clipper.search import Match, clear_cache, search

SRT = """1
00:00:01,000 --> 00:00:02,000
first line

2
00:00:03,000 --> 00:00:04,000
second line

3
00:00:05,000 --> 00:00:06,000
third line
"""


def _write(path: Path, text: str = SRT) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- single SRT --------------------------------------------------------------

def test_single_srt_lists_all_on_empty_query(tmp_path):
    srt = _write(tmp_path / "My Clip.srt")
    corpus, label = load_single_srt(srt)
    clear_cache()
    assert label == "My Clip.srt"
    rec = corpus.records[0]
    assert rec.display_name == "My Clip.srt"
    hits = search(corpus, "", allow_empty=True, group_versions=True)
    assert [m.text for m in hits] == ["first line", "second line", "third line"]
    assert all(m.display_name == "My Clip.srt" for m in hits)
    # Empty query without allow_empty still returns nothing.
    assert search(corpus, "", group_versions=True) == []


def test_single_srt_missing_file(tmp_path):
    with pytest.raises(DatasetError):
        load_single_srt(tmp_path / "nope.srt")


# --- directory + master cross-reference --------------------------------------

def _master(tmp_path) -> Corpus:
    rec = EpisodeRecord(
        episode_id="show-a_s01e01_bd", srt_path="A/ep.srt", category="series",
        dub_type="original", show_slug="show-a", show_title_en="Show A",
        show_title_zh=None, year=2001, season=1, episodes=[1], source="BD",
        sync_variant_key="show-a_s01e01", langs=["yue"], srt_sha256="x",
        media_path="A/video.mkv", media_flags=[], exclusion=None,
    )
    return Corpus(records=[rec], corpus_root=tmp_path, media_root=tmp_path / "media")


def test_directory_matches_master_show_and_reuses_media(tmp_path):
    d = tmp_path / "srts"
    d.mkdir()
    _write(d / "Show A.S01E01.BD.yue.srt")
    _write(d / "random-notes.srt")
    corpus, label = load_directory(d, _master(tmp_path))
    by_name = {r.srt_path: r for r in corpus.records}

    matched = by_name["Show A.S01E01.BD.yue.srt"]
    assert matched.show_title_en == "Show A"
    assert matched.season == 1 and matched.episodes == [1]
    assert matched.media_path == "A/video.mkv"      # reused from master
    assert matched.display_name is None             # shows show/episode, not filename
    assert corpus.media_root == tmp_path / "media"  # so the reused media resolves

    unmatched = by_name["random-notes.srt"]
    assert unmatched.display_name == "random-notes.srt"
    assert unmatched.media_path is None


def test_directory_empty(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(DatasetError):
        load_directory(d, None)


# --- bookmarks ---------------------------------------------------------------

def _bookmarks_json(idxs):
    return json.dumps({"bookmarks": [{"idx": i, "txt": ""} for i in idxs]})


def test_bookmarks_selects_only_referenced_lines(tmp_path):
    srt = _write(tmp_path / "Ep.srt")
    bm = tmp_path / "Ep.srt.SE.bookmarks"
    bm.write_text(_bookmarks_json([0, 2]), encoding="utf-8")
    corpus, label = load_bookmarks(bm, None)
    clear_cache()
    rec = corpus.records[0]
    # SubtitleEdit idx is 0-based: idx 0 -> SRT "1" (first), idx 2 -> SRT "3".
    assert rec.cue_ids == [0, 2]
    hits = search(corpus, "", allow_empty=True, group_versions=True)
    assert [m.text for m in hits] == ["first line", "third line"]
    assert "2 bookmark" in label


def test_bookmarks_use_printed_counter_when_numbering_is_offset(tmp_path):
    # A file whose counters don't start at 1: idx must still resolve via the
    # printed number (idx + 1), not by position.
    srt = _write(tmp_path / "Ep.srt", SRT.replace("1\n00:00:01", "4\n00:00:01"))
    bm = tmp_path / "Ep.srt.SE.bookmarks"
    bm.write_text(_bookmarks_json([1]), encoding="utf-8")
    corpus, _ = load_bookmarks(bm, None)
    clear_cache()
    # idx 1 -> printed "2" -> the second cue.
    assert corpus.records[0].cue_ids == [1]
    hits = search(corpus, "", allow_empty=True, group_versions=True)
    assert [m.text for m in hits] == ["second line"]


def test_bookmarks_missing_paired_srt(tmp_path):
    bm = tmp_path / "Ghost.srt.SE.bookmarks"
    bm.write_text(_bookmarks_json([1]), encoding="utf-8")
    with pytest.raises(DatasetError):
        load_bookmarks(bm, None)


def test_bookmarks_not_a_bookmarks_file(tmp_path):
    srt = _write(tmp_path / "Ep.srt")
    with pytest.raises(DatasetError):
        load_bookmarks(srt, None)


# --- merging consecutive bookmarks into one entry ----------------------------

def test_group_bookmark_indices_thresholds():
    # Default: only back-to-back bookmarks share an entry.
    assert group_bookmark_indices([9, 10, 13, 17, 19], 1) == [[9, 10], [13], [17], [19]]
    # Threshold 3 (the README/UI example, in 1-based line numbers: bookmarks at
    # 10, 11, 14, 18, 20 -> entries 10–14 and 18–20) fills in the gaps.
    assert group_bookmark_indices([9, 10, 13, 17, 19], 3) == \
        [[9, 10, 11, 12, 13], [17, 18, 19]]
    # Unsorted/duplicate input, and a threshold below 1, are tolerated.
    assert group_bookmark_indices([5, 4, 4], 0) == [[4, 5]]
    assert group_bookmark_indices([], 2) == []


def _numbered_srt(count: int) -> str:
    return "\n".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},500\nline {n}\n"
                     for n in range(1, count + 1))


def test_consecutive_bookmarks_become_one_entry(tmp_path):
    _write(tmp_path / "Ep.srt", _numbered_srt(6))
    bm = tmp_path / "Ep.srt.SE.bookmarks"
    bm.write_text(_bookmarks_json([0, 1, 4]), encoding="utf-8")   # lines 1, 2, 5
    corpus, label = load_bookmarks(bm, None)
    clear_cache()
    assert "3 bookmark(s)" in label and "2 entries" in label

    hits = search(corpus, "", allow_empty=True, group_versions=True)
    assert len(hits) == 2
    merged, single = hits
    # The merged entry is anchored on its first line and spans through the last;
    # both lines stay separate cues, listed under group_lines.
    assert merged.text == "line 1" and merged.start == 1.0 and merged.end == 1.5
    assert merged.group_end == 2.5
    assert list(merged.group_lines) == ["line 1", "line 2"]
    # An unmerged bookmark is an ordinary single-line result.
    assert single.text == "line 5" and single.group_end is None
    assert single.group_lines == ()


def test_merge_threshold_fills_in_unbookmarked_lines(tmp_path):
    _write(tmp_path / "Ep.srt", _numbered_srt(25))
    bm = tmp_path / "Ep.srt.SE.bookmarks"
    bm.write_text(_bookmarks_json([9, 10, 13, 17, 19]), encoding="utf-8")
    corpus, _ = load_bookmarks(bm, None, merge_threshold=3)
    clear_cache()
    hits = search(corpus, "", allow_empty=True, group_versions=True)
    assert [(h.start, h.group_end) for h in hits] == [(10.0, 14.5), (18.0, 20.5)]
    assert list(hits[0].group_lines) == [f"line {n}" for n in range(10, 15)]
    assert list(hits[1].group_lines) == [f"line {n}" for n in range(18, 21)]
    # The filled-in lines are part of the entry, so they're searchable too...
    assert corpus.records[0].cue_ids == [9, 10, 11, 12, 13, 17, 18, 19]
    # ...and a hit on one of them returns the whole entry, not a separate row.
    hits = search(corpus, "line 12", group_versions=True)
    assert len(hits) == 1 and hits[0].text == "line 10"


def test_merged_entry_defaults_to_a_window_around_the_whole_run(tmp_path, monkeypatch):
    _write(tmp_path / "Ep.srt", _numbered_srt(4))
    bm = tmp_path / "Ep.srt.SE.bookmarks"
    bm.write_text(_bookmarks_json([0, 1]), encoding="utf-8")
    corpus, _ = load_bookmarks(bm, None)
    clear_cache()
    merged = search(corpus, "", allow_empty=True, group_versions=True)[0]

    cuts = []
    monkeypatch.setattr(clips, "_select_audio_track", lambda src: 0)
    monkeypatch.setattr(clips, "cut_segment", lambda src, s, e, out, **kw: (
        cuts.append((s, e)), Path(out).write_bytes(b"")))
    monkeypatch.setattr(clips, "concat", lambda files, out, **kw: Path(out).write_bytes(b""))
    monkeypatch.setattr(clips, "mux_subtitles", lambda v, s, out, **kw: Path(out).write_bytes(b""))

    video = tmp_path / "ep.mkv"
    video.write_bytes(b"")
    # The web UI sends an explicit window plus one extra cue per further line of
    # the entry (the same shape as manually widening a window); the lines are
    # never fused, so each keeps its own subtitle block.
    report = clips.generate(
        [ClipRequest(match=merged, win_start=0.5, win_end=3.0,
                     extra_cues=(CueEntry(2.0, 2.5, "line 2"),),
                     video_override=str(video))],
        None, tmp_path / "out.mkv",
    )
    assert cuts == [(0.5, 3.0)]
    srt = report.srt.read_text(encoding="utf-8")
    assert "00:00:00,500 --> 00:00:01,000\nline 1" in srt
    assert "00:00:01,500 --> 00:00:02,000\nline 2" in srt

    cuts.clear()
    clips.generate([ClipRequest(match=merged, video_override=str(video))],
                   None, tmp_path / "out2.mkv", pad_s=0.5)
    # Without an explicit window, padding still wraps the whole entry (lines
    # 1–2), not just its first line.
    assert cuts == [(0.5, 3.0)]


# --- cue_ids restriction in search ------------------------------------------

def test_cue_ids_restrict_matches(tmp_path):
    srt = _write(tmp_path / "ep.srt")
    rec = EpisodeRecord(
        episode_id="x", srt_path="ep.srt", category="custom", dub_type="original",
        show_slug="x", show_title_en="ep.srt", show_title_zh=None, year=None,
        season=None, episodes=[], source=None, sync_variant_key="x", langs=[],
        srt_sha256="", cue_ids=[1],
    )
    corpus = Corpus(records=[rec], corpus_root=tmp_path, media_root=None)
    clear_cache()
    # "line" matches all three cues, but cue_ids restricts to index 1 only.
    hits = search(corpus, "line", group_versions=True)
    assert [m.text for m in hits] == ["second line"]


# --- separate (per-clip) generation ------------------------------------------

def test_generate_separate_writes_one_file_per_clip(tmp_path, monkeypatch):
    # Fake the ffmpeg steps so no real encoding happens; each just creates a file.
    monkeypatch.setattr(clips, "_select_audio_track", lambda src: 0)
    monkeypatch.setattr(clips, "cut_segment",
                        lambda src, s, e, out, **kw: Path(out).write_bytes(b""))
    monkeypatch.setattr(clips, "concat",
                        lambda files, out, **kw: Path(out).write_bytes(b""))
    monkeypatch.setattr(clips, "mux_subtitles",
                        lambda v, s, out, **kw: Path(out).write_bytes(b""))

    srcs = []
    items = []
    for n in range(2):
        v = tmp_path / f"src{n}.mkv"
        v.write_bytes(b"")
        srcs.append(v)
        m = Match(episode_id=f"e{n}", show_slug="s", show_title="S", season=1,
                  episodes=(1,), cue_index=0, start=1.0, end=2.0, text=f"line {n}",
                  srt_path="x.srt", media_path=None, media_status="video")
        items.append(ClipRequest(match=m, video_override=str(v)))

    out = tmp_path / "clip.mkv"
    report = clips.generate(items, None, out, separate=True)
    assert len(report.outputs) == 2
    assert [p.name for p in report.outputs] == ["clip-01.mkv", "clip-02.mkv"]
    for o, s in zip(report.outputs, report.srts):
        assert o.is_file() and s.is_file()
        assert s.name.endswith(".srt")
    assert not out.is_file()   # no single combined output in separate mode
