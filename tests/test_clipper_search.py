"""Search behaviour for the standalone subtitle_clipper module."""

from pathlib import Path

from subtitle_clipper.manifest import EpisodeRecord
from subtitle_clipper.corpus import Corpus
from subtitle_clipper.search import clear_cache, search

SRT_A = """1
00:00:10,000 --> 00:00:12,500
邊個話你知㗎

2
00:00:14,000 --> 00:00:16,000
我唔知呀
"""

SRT_B = """1
00:00:01,000 --> 00:00:03,000
邊度呀

2
00:00:05,000 --> 00:00:07,000
食咗飯未
"""


def _record(episode_id, srt_rel, show_slug, title, *, media_path=None):
    return EpisodeRecord(
        episode_id=episode_id, srt_path=srt_rel, category="series", dub_type="original",
        show_slug=show_slug, show_title_en=title, show_title_zh=None, year=2000,
        season=1, episodes=[1], source="BD", sync_variant_key=f"{show_slug}_s01e01",
        langs=["yue"], srt_sha256="x", media_path=media_path, media_flags=[], exclusion=None,
    )


def _corpus(tmp_path, *, media_root=None):
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "ep.srt").write_text(SRT_A, encoding="utf-8")
    (tmp_path / "B" / "ep.srt").write_text(SRT_B, encoding="utf-8")
    records = [
        _record("show-a_s01e01_bd", "A/ep.srt", "show-a", "Show A"),
        _record("show-b_s01e01_bd", "B/ep.srt", "show-b", "Show B"),
    ]
    clear_cache()
    return Corpus(records=records, corpus_root=tmp_path, media_root=media_root), records


def test_substring_match_across_shows(tmp_path):
    corpus, _ = _corpus(tmp_path)
    hits = search(corpus, "邊")
    texts = [m.text for m in hits]
    assert "邊個話你知㗎" in texts
    assert "邊度呀" in texts
    assert all(m.media_status == "unlinked" for m in hits)


def test_top_limit(tmp_path):
    corpus, _ = _corpus(tmp_path)
    assert len(search(corpus, "呀", top=1)) == 1


def test_regex(tmp_path):
    corpus, _ = _corpus(tmp_path)
    hits = search(corpus, r"^食", regex=True)
    assert [m.text for m in hits] == ["食咗飯未"]


def test_only_and_exclude_shows(tmp_path):
    corpus, _ = _corpus(tmp_path)
    only = search(corpus, "邊", only_shows=("show-a",))
    assert {m.show_slug for m in only} == {"show-a"}
    excl = search(corpus, "邊", exclude_shows=("Show A",))
    assert {m.show_slug for m in excl} == {"show-b"}


def test_require_media_filters_unlinked(tmp_path):
    video = tmp_path / "media" / "a.mkv"
    video.parent.mkdir()
    video.write_bytes(b"")
    corpus, records = _corpus(tmp_path, media_root=tmp_path / "media")
    records[0].media_path = "a.mkv"          # Show A now has a video file
    assert search(corpus, "邊", require_media=True) and \
        all(m.show_slug == "show-a" for m in search(corpus, "邊", require_media=True))


# --- multi-version collapse --------------------------------------------------

SRT_VER = """1
00:00:{sec:02d},000 --> 00:00:{end:02d},000
係喎
"""


def _versioned_corpus(tmp_path):
    """One episode released as BD (line at 0:10) and DVD (line at 0:20), same
    sync_variant_key."""
    (tmp_path / "BD").mkdir()
    (tmp_path / "DVD").mkdir()
    (tmp_path / "BD" / "ep.srt").write_text(SRT_VER.format(sec=10, end=12), encoding="utf-8")
    (tmp_path / "DVD" / "ep.srt").write_text(SRT_VER.format(sec=20, end=22), encoding="utf-8")
    bd = _record("show_s01e01_bd", "BD/ep.srt", "show", "Show")
    dvd = _record("show_s01e01_dvd", "DVD/ep.srt", "show", "Show")
    dvd.source = "DVD"                          # EpisodeRecord is a plain dataclass
    bd.sync_variant_key = dvd.sync_variant_key = "show_s01e01"  # same episode identity
    clear_cache()
    return Corpus(records=[bd, dvd], corpus_root=tmp_path, media_root=None)


def test_group_versions_collapses_to_one_switchable_result(tmp_path):
    corpus = _versioned_corpus(tmp_path)
    flat = search(corpus, "係喎")
    assert len(flat) == 2                      # ungrouped: one per version

    grouped = search(corpus, "係喎", group_versions=True)
    assert len(grouped) == 1                    # collapsed into a single item
    m = grouped[0]
    assert m.source == "BD"                     # preferred version is representative
    assert m.start == 10.0
    sources = [v["source"] for v in m.versions]
    assert sources == ["BD", "DVD"]             # both offered, preferred first
    dvd = next(v for v in m.versions if v["source"] == "DVD")
    assert dvd["start"] == 20.0 and dvd["episode_id"] == "show_s01e01_dvd"


def test_group_versions_round_trips_through_dict(tmp_path):
    from subtitle_clipper.search import Match
    corpus = _versioned_corpus(tmp_path)
    m = search(corpus, "係喎", group_versions=True)[0]
    d = m.to_dict()
    assert len(d["versions"]) == 2
    again = Match.from_dict(d)
    assert again.sync_variant_key == "show_s01e01"
    assert [v["source"] for v in again.versions] == ["BD", "DVD"]
