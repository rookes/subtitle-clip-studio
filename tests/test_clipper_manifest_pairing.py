"""SRT <-> media pairing over a real directory tree (subtitle_clipper.manifest).

Covers the parts of `pair_media` that decide *which* file an episode gets:
locating the show folder, honouring season folders, and choosing between
several candidate files for one episode.
"""

from dataclasses import replace
from pathlib import Path

from subtitle_clipper.config import CorpusConfig, MediaMapConfig
from subtitle_clipper.manifest import pair_media, scan_corpus

SRT = "1\n00:00:01,000 --> 00:00:02,000\nline\n"


def _touch(*paths: Path) -> None:
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")


def _corpus(root: Path) -> CorpusConfig:
    return CorpusConfig(
        root=root,
        include_roots=("Series/Dubbed",),
        exclude_globs=(),
    )


def _srt(root: Path, show: str, season_folder: str, name: str) -> None:
    path = root / "Series" / "Dubbed" / show / season_folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SRT, encoding="utf-8")


def _pair(subs: Path, media: Path) -> dict[str, str | None]:
    """Scan `subs`, pair against `media`, return {srt filename: media filename}."""
    records = scan_corpus(_corpus(subs))
    pair_media(records, MediaMapConfig(media_root=media), subs)
    return {
        Path(r.srt_path).name: (Path(r.media_path).name if r.media_path else None)
        for r in records if r.exclusion is None
    }


# --- season folders ----------------------------------------------------------

def test_season_folder_keeps_a_seasons_episodes_to_itself(tmp_path):
    # Attack on Titan: video for seasons 1 and 3 only, files numbered without an
    # SxxEyy tag. S2E05 must not fall through to the S1 folder's episode 5.
    subs, media = tmp_path / "subs", tmp_path / "media"
    show = "Attack on Titan -- 進擊的巨人 (2013)"
    _srt(subs, show, "Season 1 - BD", "AoT.S1E05.BD.yue.srt")
    _srt(subs, show, "Season 2 - BD", "AoT.S2E05.BD.yue.srt")
    _touch(*(media / "Series" / "Dubbed" / show / "S1" / f"Show - {n:02d}.mkv"
             for n in range(1, 26)))

    paired = _pair(subs, media)
    assert paired["AoT.S1E05.BD.yue.srt"] == "Show - 05.mkv"
    assert paired["AoT.S2E05.BD.yue.srt"] is None


def test_flat_media_folder_still_matches_any_season(tmp_path):
    # No season folder anywhere in the media tree -> nothing says the files
    # belong to a particular season, so they stay available to season 1.
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Show (2001)", "Season 1", "Show.S1E02.BD.yue.srt")
    _touch(media / "Series" / "Dubbed" / "Show (2001)" / "02.mkv")

    assert _pair(subs, media)["Show.S1E02.BD.yue.srt"] == "02.mkv"


def test_series_absolute_numbering_inside_a_season_folder(tmp_path):
    # The S3 folder is numbered across the whole series (38-59). Seasons 1 and 2
    # run 25 + 12 episodes, so 38 *is* S3E01 and the offset is provable.
    subs, media = tmp_path / "subs", tmp_path / "media"
    show = "Show (2013)"
    for n in range(1, 26):
        _srt(subs, show, "Season 1", f"Show.S1E{n:02d}.BD.yue.srt")
    for n in range(1, 13):
        _srt(subs, show, "Season 2", f"Show.S2E{n:02d}.BD.yue.srt")
    _srt(subs, show, "Season 3", "Show.S3E01.BD.yue.srt")
    _srt(subs, show, "Season 3", "Show.S3E02.BD.yue.srt")
    _touch(*(media / "Series" / "Dubbed" / show / "S3" / f"Show - {n}.mkv"
             for n in range(38, 60)))

    paired = _pair(subs, media)
    assert paired["Show.S3E01.BD.yue.srt"] == "Show - 38.mkv"
    assert paired["Show.S3E02.BD.yue.srt"] == "Show - 39.mkv"


def test_partial_season_is_not_read_as_absolute_numbering(tmp_path):
    # A lone "Show - 07.mkv" in an S1 folder is a half-finished download, not a
    # series-wide numbering: episode 1 of season 1 is episode 1, so no offset
    # is possible and the episode stays unlinked.
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Show (1992)", "Season 1", "Show.S1E01.BD.yue.srt")
    _touch(media / "Series" / "Dubbed" / "Show (1992)" / "S1" / "Show - 07.mkv")

    assert _pair(subs, media)["Show.S1E01.BD.yue.srt"] is None


# --- locating the show folder ------------------------------------------------

def test_show_folder_matches_on_title_when_the_name_differs(tmp_path):
    # Subtitles keep the decorated name, the media folder is just "Bluey".
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Bluey -- 妙妙犬保怡 (2018)", "Season 1",
         "Bluey - S1E01 The Magic Xylophone.yue.srt")
    _touch(media / "Series" / "Dubbed" / "Bluey" / "S1" / "S01E001_Xylophone.mp4")

    paired = _pair(subs, media)
    assert paired["Bluey - S1E01 The Magic Xylophone.yue.srt"] == "S01E001_Xylophone.mp4"


def test_show_folder_matches_across_dub_type(tmp_path):
    # Subtitles filed under Dubbed, the video under Original: still one show.
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Show -- 中文 (2001)", "Season 1", "Show.S1E01.BD.yue.srt")
    _touch(media / "Series" / "Original" / "Show" / "S1" / "Show.S01E01.mkv")

    assert _pair(subs, media)["Show.S1E01.BD.yue.srt"] == "Show.S01E01.mkv"


def test_show_folder_with_a_different_year_is_not_the_same_show(tmp_path):
    # A remake's folder must not be borrowed for the original.
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Show (2001)", "Season 1", "Show.S1E01.BD.yue.srt")
    _touch(media / "Series" / "Dubbed" / "Show (2019)" / "S1" / "Show.S01E01.mkv")

    assert _pair(subs, media)["Show.S1E01.BD.yue.srt"] is None


def test_show_folder_year_breaks_a_title_tie(tmp_path):
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Show (2001)", "Season 1", "Show.S1E01.BD.yue.srt")
    _touch(media / "Series" / "Dubbed" / "Show" / "S1" / "wrong.S01E01.mkv",
           media / "Series" / "Dubbed" / "Show (2001)" / "S1" / "right.S01E01.mkv")

    assert _pair(subs, media)["Show.S1E01.BD.yue.srt"] == "right.S01E01.mkv"


# --- choosing between candidate files ----------------------------------------

def test_same_rip_in_two_containers_is_not_ambiguous(tmp_path):
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Show (2013)", "Season 1", "Show.S1E01.BD.yue.srt")
    stem = media / "Series" / "Dubbed" / "Show (2013)" / "S1" / "Show - 01"
    _touch(stem.with_suffix(".mkv"), stem.with_suffix(".mp4"))

    assert _pair(subs, media)["Show.S1E01.BD.yue.srt"] == "Show - 01.mkv"


def test_derived_copies_in_a_subfolder_lose_to_the_season_folder(tmp_path):
    # Bluey keeps downmixed and hardsubbed copies under S1/downmix/.
    subs, media = tmp_path / "subs", tmp_path / "media"
    _srt(subs, "Bluey -- 妙妙犬保怡 (2018)", "Season 1", "Bluey - S1E01 Xylophone.yue.srt")
    season = media / "Series" / "Dubbed" / "Bluey" / "S1"
    _touch(season / "S01E001_Xylophone.mp4",
           season / "downmix" / "S01E001_Xylophone.mp4",
           season / "downmix" / "S01E001_Xylophone.subbed.mp4")

    paired = _pair(subs, media)
    assert paired["Bluey - S1E01 Xylophone.yue.srt"] == "S01E001_Xylophone.mp4"
