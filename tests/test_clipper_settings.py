"""Per-user Subtitle Clip Studio settings (subtitle_clipper.settings)."""

from pathlib import Path

from subtitle_clipper.settings import (
    MAX_BOOKMARK_MERGE,
    QUALITY_CRF,
    RESOLUTIONS,
    ClipperSettings,
    load_settings,
    save_settings,
)


def test_defaults_when_file_missing(tmp_path):
    s = load_settings(tmp_path / "nope.toml")
    assert s.subtitle_root is None and s.media_root is None
    assert s.resolution == "720p" and s.quality == "medium"
    assert s.container == "mp4" and s.suffix() == ".mp4"
    assert s.subtitle_root_path() is None and s.media_root_path() is None


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "clipper.toml"
    save_settings(ClipperSettings(subtitle_root="D:/Subs", media_root="D:/Media",
                                  resolution="1080p", quality="high"), path)
    s = load_settings(path)
    assert s.subtitle_root == "D:/Subs" and s.media_root == "D:/Media"
    assert s.resolution == "1080p" and s.quality == "high"
    assert s.subtitle_root_path() == Path("D:/Subs")
    assert s.media_root_path() == Path("D:/Media")


def test_empty_subtitle_root_is_none(tmp_path):
    path = tmp_path / "clipper.toml"
    path.write_text('[media]\nsubtitle_root = "  "\n', encoding="utf-8")
    assert load_settings(path).subtitle_root is None


def test_windows_backslash_path_survives_round_trip(tmp_path):
    path = tmp_path / "clipper.toml"
    save_settings(ClipperSettings(media_root=r"C:\Canto\Media"), path)
    assert load_settings(path).media_root == r"C:\Canto\Media"


def test_invalid_enums_clamp_to_defaults(tmp_path):
    path = tmp_path / "clipper.toml"
    path.write_text(
        '[output]\nresolution = "8k"\nquality = "ultra"\ncontainer = "avi"\n',
        encoding="utf-8",
    )
    s = load_settings(path)
    assert s.resolution == "720p" and s.quality == "medium"
    # An unsupported container must never reach ffmpeg as an output suffix.
    assert s.container == "mp4" and s.suffix() == ".mp4"


def test_container_round_trips_and_drives_the_suffix(tmp_path):
    path = tmp_path / "clipper.toml"
    save_settings(ClipperSettings(container="mkv"), path)
    s = load_settings(path)
    assert s.container == "mkv" and s.suffix() == ".mkv"


def test_empty_media_root_is_none(tmp_path):
    path = tmp_path / "clipper.toml"
    path.write_text('[media]\nmedia_root = "  "\n', encoding="utf-8")
    assert load_settings(path).media_root is None


def test_dimensions_and_crf_mapping():
    assert ClipperSettings(resolution="480p").dimensions() == RESOLUTIONS["480p"]
    assert ClipperSettings(quality="high").crf() == QUALITY_CRF["high"]
    assert ClipperSettings(quality="low").crf() > ClipperSettings(quality="high").crf()


def test_bookmark_merge_round_trips(tmp_path):
    path = tmp_path / "clipper.toml"
    assert load_settings(tmp_path / "nope.toml").bookmark_merge == 1
    save_settings(ClipperSettings(bookmark_merge=3), path)
    assert load_settings(path).bookmark_merge == 3


def test_bookmark_merge_clamps_out_of_range_values(tmp_path):
    path = tmp_path / "clipper.toml"
    for written, expected in ((0, 1), (-4, 1), (1000, MAX_BOOKMARK_MERGE)):
        path.write_text(f"[bookmarks]\nmerge_threshold = {written}\n", encoding="utf-8")
        assert load_settings(path).bookmark_merge == expected
    # A non-numeric value falls back to the default rather than raising.
    path.write_text('[bookmarks]\nmerge_threshold = "lots"\n', encoding="utf-8")
    assert load_settings(path).bookmark_merge == 1


def test_malformed_toml_returns_defaults(tmp_path):
    path = tmp_path / "clipper.toml"
    path.write_text("this is = not [valid toml", encoding="utf-8")
    assert load_settings(path) == ClipperSettings()
