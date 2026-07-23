"""Custom datasets (single SRT / directory / bookmarks), empty-query search,
cue_id restriction, and per-clip (separate) generation."""

import json
from pathlib import Path

import pytest

from subtitle_clipper import clips
from subtitle_clipper.clips import ClipRequest
from subtitle_clipper.corpus import Corpus
from subtitle_clipper.datasets import (
    DatasetError,
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
    bm.write_text(_bookmarks_json([1, 3]), encoding="utf-8")
    corpus, label = load_bookmarks(bm, None)
    clear_cache()
    rec = corpus.records[0]
    # idx 1 -> cue.index 0 (first), idx 3 -> cue.index 2 (third).
    assert rec.cue_ids == [0, 2]
    hits = search(corpus, "", allow_empty=True, group_versions=True)
    assert [m.text for m in hits] == ["first line", "third line"]
    assert "2 bookmark" in label


def test_bookmarks_missing_paired_srt(tmp_path):
    bm = tmp_path / "Ghost.srt.SE.bookmarks"
    bm.write_text(_bookmarks_json([1]), encoding="utf-8")
    with pytest.raises(DatasetError):
        load_bookmarks(bm, None)


def test_bookmarks_not_a_bookmarks_file(tmp_path):
    srt = _write(tmp_path / "Ep.srt")
    with pytest.raises(DatasetError):
        load_bookmarks(srt, None)


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
