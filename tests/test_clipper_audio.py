"""Language-aware audio-track selection (subtitle_clipper.audio + AudioConfig)."""

from subtitle_clipper.audio import select_track, subtitle_language_tag
from subtitle_clipper.config import AudioConfig


def _stream(language=None, title=None):
    tags = {}
    if language is not None:
        tags["language"] = language
    if title is not None:
        tags["title"] = title
    return {"tags": tags}


def test_prefers_primary_language_over_fallback():
    streams = [_stream(language="jpn"), _stream(language="cmn"), _stream(language="yue")]
    assert select_track(streams) == 2   # Cantonese wins over Mandarin/Japanese


def test_primary_title_keyword_matches():
    streams = [_stream(language="und"), _stream(title="Cantonese 5.1")]
    assert select_track(streams) == 1


def test_falls_back_to_any_chinese_track():
    streams = [_stream(language="eng"), _stream(language="zh"), _stream(language="fra")]
    assert select_track(streams) == 1   # no yue, so first Chinese track


def test_no_chinese_returns_zero():
    streams = [_stream(language="eng"), _stream(language="jpn")]
    assert select_track(streams) == 0   # let ffmpeg choose


def test_empty_streams_returns_zero():
    assert select_track([]) == 0


def test_custom_config_localizes_selection():
    # A Japanese-first corpus: primary=jpn, so the Japanese track is chosen even
    # though a Chinese track is present.
    cfg = AudioConfig(
        primary_lang_codes=("jpn",),
        primary_title_keywords=("japanese",),
        fallback_lang_codes=("zh",),
        fallback_title_keywords=(),
        subtitle_language_tag="jpn",
    )
    streams = [_stream(language="zh"), _stream(language="jpn")]
    assert select_track(streams, cfg) == 1


def test_audio_config_load_defaults_when_absent(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text('[corpus]\nroot = "."\n', encoding="utf-8")
    cfg = AudioConfig.load(path)
    assert cfg.primary_lang_codes == ("yue",)
    assert cfg.subtitle_language_tag == "yue"


def test_audio_config_load_reads_table(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text(
        '[audio]\n'
        'primary_lang_codes = ["jpn"]\n'
        'subtitle_language_tag = "jpn"\n',
        encoding="utf-8",
    )
    cfg = AudioConfig.load(path)
    assert cfg.primary_lang_codes == ("jpn",)
    assert cfg.subtitle_language_tag == "jpn"


def test_default_subtitle_language_tag():
    # Module default (unconfigured) is Cantonese.
    assert subtitle_language_tag() == "yue"
