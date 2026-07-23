"""Server-side directory browsing and series re-linking (subtitle_clipper.corpus)."""

from subtitle_clipper.corpus import list_dir, match_videos_in_directory


def test_list_dir_dirs_first_then_files(tmp_path):
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "b.mkv").write_bytes(b"")
    (tmp_path / "a.txt").write_bytes(b"")
    entries = list_dir(tmp_path)
    names = [e["name"] for e in entries]
    assert names[0] == ".."
    assert names[1:3] == ["alpha", "zeta"]
    assert "b.mkv" in names and "a.txt" in names


def test_list_dir_videos_only_filters_non_video_files(tmp_path):
    (tmp_path / "b.mkv").write_bytes(b"")
    (tmp_path / "a.txt").write_bytes(b"")
    entries = list_dir(tmp_path, videos_only=True)
    names = [e["name"] for e in entries]
    assert "b.mkv" in names
    assert "a.txt" not in names


def test_match_videos_in_directory_by_season_episode(tmp_path):
    (tmp_path / "Show.S01E01.mkv").write_bytes(b"")
    (tmp_path / "Show.S01E02.mkv").write_bytes(b"")
    items = [
        {"episode_id": "show_s01e01_bd", "season": 1, "episodes": [1]},
        {"episode_id": "show_s01e02_bd", "season": 1, "episodes": [2]},
        {"episode_id": "show_s01e03_bd", "season": 1, "episodes": [3]},
    ]
    mapping = match_videos_in_directory(items, tmp_path)
    assert mapping["show_s01e01_bd"].endswith("Show.S01E01.mkv")
    assert mapping["show_s01e02_bd"].endswith("Show.S01E02.mkv")
    assert mapping["show_s01e03_bd"] is None


def test_match_videos_in_directory_movie_single_file(tmp_path):
    (tmp_path / "Movie.2020.mkv").write_bytes(b"")
    items = [{"episode_id": "movie_2020", "season": None, "episodes": []}]
    mapping = match_videos_in_directory(items, tmp_path)
    assert mapping["movie_2020"].endswith("Movie.2020.mkv")


def test_match_videos_in_directory_missing_directory(tmp_path):
    items = [{"episode_id": "x", "season": 1, "episodes": [1]}]
    mapping = match_videos_in_directory(items, tmp_path / "does-not-exist")
    assert mapping["x"] is None


def test_match_videos_prefers_version_folder(tmp_path):
    # Same episode present under BD / DVD / Netflix season folders; the item's
    # source should pin the matching version's file.
    for folder in ("Season 1 - BD (sync)", "Season 1 - DVD (sync)", "Season 1 - Netflix"):
        d = tmp_path / folder
        d.mkdir()
        (d / "Show.S01E01.mkv").write_bytes(b"")
    bd = match_videos_in_directory(
        [{"episode_id": "e1", "season": 1, "episodes": [1], "source": "BD"}], tmp_path)
    dvd = match_videos_in_directory(
        [{"episode_id": "e1", "season": 1, "episodes": [1], "source": "DVD"}], tmp_path)
    nf = match_videos_in_directory(
        [{"episode_id": "e1", "season": 1, "episodes": [1], "source": "NF"}], tmp_path)
    assert "BD (sync)" in bd["e1"]
    assert "DVD (sync)" in dvd["e1"]
    assert "Netflix" in nf["e1"]


def test_match_videos_generic_season_folder_matches_regardless_of_version(tmp_path):
    # A plain S1 folder with a single file: match it whatever the item's source.
    d = tmp_path / "S1"
    d.mkdir()
    (d / "Show.S01E01.mkv").write_bytes(b"")
    mapping = match_videos_in_directory(
        [{"episode_id": "e1", "season": 1, "episodes": [1], "source": "BD"}], tmp_path)
    assert mapping["e1"].endswith("Show.S01E01.mkv")


def test_match_videos_ambiguous_without_version_hint_is_none(tmp_path):
    # Two version folders but no source hint → still ambiguous, left unlinked.
    for folder in ("Season 1 - BD (sync)", "Season 1 - DVD (sync)"):
        d = tmp_path / folder
        d.mkdir()
        (d / "Show.S01E01.mkv").write_bytes(b"")
    mapping = match_videos_in_directory(
        [{"episode_id": "e1", "season": 1, "episodes": [1]}], tmp_path)
    assert mapping["e1"] is None


# --- combined season+episode codes (Avatar's 101.mkv -> S1E1) ----------------

def _touch(*paths):
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")


def test_relinker_matches_combined_codes(tmp_path):
    # Avatar-style: S1/101.mkv should link to S1E1, S1E2, ...
    s1 = tmp_path / "S1"
    _touch(s1 / "101.mkv", s1 / "102.mkv")
    items = [
        {"episode_id": "avatar_s01e01", "season": 1, "episodes": [1]},
        {"episode_id": "avatar_s01e02", "season": 1, "episodes": [2]},
    ]
    mapping = match_videos_in_directory(items, tmp_path)
    assert mapping["avatar_s01e01"].endswith("101.mkv")
    assert mapping["avatar_s01e02"].endswith("102.mkv")


def test_relinker_combined_only_when_season_unmatched(tmp_path):
    # Season 1 already matches normally (S01E01) -> 102.mkv must NOT be reread
    # as S1E2; the combined fallback is suppressed for a season that matched.
    _touch(tmp_path / "Show.S01E01.mkv", tmp_path / "102.mkv")
    items = [
        {"episode_id": "e1", "season": 1, "episodes": [1]},
        {"episode_id": "e2", "season": 1, "episodes": [2]},
    ]
    mapping = match_videos_in_directory(items, tmp_path)
    assert mapping["e1"].endswith("Show.S01E01.mkv")
    assert mapping["e2"] is None


def test_relinker_no_combined_when_zero_padded(tmp_path):
    # 001.mkv present -> plain 3-digit numbering: episode 1 is 001.mkv, and 101
    # is episode 101 (never reread as S1E1).
    _touch(tmp_path / "001.mkv", tmp_path / "101.mkv")
    items = [
        {"episode_id": "e1", "season": 1, "episodes": [1]},
        {"episode_id": "e101", "season": 1, "episodes": [101]},
    ]
    mapping = match_videos_in_directory(items, tmp_path)
    assert mapping["e1"].endswith("001.mkv")
    assert mapping["e101"].endswith("101.mkv")
