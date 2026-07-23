"""Typed loaders for the TOML configs in config/.

Every relative path inside a config file is resolved against that file's own
directory, so the tool works regardless of the current working directory.

This is a self-contained copy of the corpus-config loader (no external project
dependency): it only carries the pieces Subtitle Clip Studio actually needs —
corpus scanning rules, SRT↔media pairing, and audio-track language preferences.
"""

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _resolve(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


@dataclass(frozen=True)
class SeriesExclusion:
    """A whole series to drop from the corpus (editorial/quality/licensing).

    Matches an SRT by its path relative to the corpus root (posix, forward
    slashes) via either `patterns` (fnmatch globs) or `dirs`. A dir containing
    "/" is a path prefix relative to root; a bare dir matches any folder name in
    the path (both case-insensitive). `name` is recorded as the manifest reason.
    """
    name: str
    patterns: tuple[str, ...] = ()
    dirs: tuple[str, ...] = ()

    def matches(self, rel: str) -> bool:
        if any(fnmatch.fnmatch(rel, p) for p in self.patterns):
            return True
        low = rel.lower()
        parts = low.split("/")
        for d in self.dirs:
            d = d.strip("/").lower()
            if not d:
                continue
            if "/" in d:
                if low == d or low.startswith(d + "/"):
                    return True
            elif d in parts:
                return True
        return False

    @staticmethod
    def from_raw(raw: dict) -> "SeriesExclusion":
        def as_tuple(value) -> tuple[str, ...]:
            if value is None:
                return ()
            return (value,) if isinstance(value, str) else tuple(value)

        return SeriesExclusion(
            name=raw["name"],
            patterns=as_tuple(raw.get("pattern")) + as_tuple(raw.get("patterns")),
            dirs=as_tuple(raw.get("dir")) + as_tuple(raw.get("dirs")),
        )


@dataclass(frozen=True)
class CorpusConfig:
    root: Path
    include_roots: tuple[str, ...]
    exclude_globs: tuple[str, ...]
    exclude_series: tuple[SeriesExclusion, ...] = ()

    @staticmethod
    def load(path: Path) -> "CorpusConfig":
        raw = _load_toml(path)["corpus"]
        return CorpusConfig(
            root=_resolve(path.parent, raw["root"]),
            include_roots=tuple(raw["include_roots"]),
            exclude_globs=tuple(raw["exclude_globs"]),
            exclude_series=tuple(
                SeriesExclusion.from_raw(s) for s in raw.get("exclude_series", [])
            ),
        )


@dataclass(frozen=True)
class MediaMapConfig:
    media_root: Path | None
    shows: dict[str, dict] = field(default_factory=dict)

    @staticmethod
    def load(path: Path) -> "MediaMapConfig":
        raw = _load_toml(path)
        root_raw = raw.get("media_root", "")
        root = _resolve(path.parent, root_raw) if root_raw else None
        return MediaMapConfig(media_root=root, shows=raw.get("shows", {}))


# --- audio-track language preferences ----------------------------------------
# Which audio stream a generated clip uses, and the language tag written onto
# the muxed subtitle track. Defaults target Cantonese (primary) then any Chinese
# track (fallback); override the `[audio]` table in corpus.toml to localize the
# tool for a different-language corpus. See subtitle_clipper.audio.

# Language codes / title keywords for the DEFAULT (Cantonese-first) behaviour.
DEFAULT_PRIMARY_LANG_CODES = ("yue",)
DEFAULT_PRIMARY_TITLE_KEYWORDS = ("cantonese",)
DEFAULT_FALLBACK_LANG_CODES = (
    "zh", "zho", "chi", "cmn", "nan", "hak", "wuu",
    "zh-hans", "zh-hant", "zh-hk", "zh-tw", "zh-cn", "zh-sg",
)
DEFAULT_FALLBACK_TITLE_KEYWORDS = (
    "chinese", "cantonese", "mandarin", "putonghua", "guoyu", "huayu",
    "中文", "汉语", "漢語", "华语", "華語", "国语", "國語",
    "普通话", "普通話", "粤", "粵", "粵語", "粤语", "廣東話", "广东话",
)
DEFAULT_SUBTITLE_LANGUAGE_TAG = "yue"


@dataclass(frozen=True)
class AudioConfig:
    """Audio-track language selection for clip generation.

    ``primary_*`` identify the ideal track (picked first); ``fallback_*`` the
    acceptable ones (any close-enough language) used when no primary track
    exists. ``subtitle_language_tag`` is written onto the muxed subtitle stream.
    """
    primary_lang_codes: tuple[str, ...] = DEFAULT_PRIMARY_LANG_CODES
    primary_title_keywords: tuple[str, ...] = DEFAULT_PRIMARY_TITLE_KEYWORDS
    fallback_lang_codes: tuple[str, ...] = DEFAULT_FALLBACK_LANG_CODES
    fallback_title_keywords: tuple[str, ...] = DEFAULT_FALLBACK_TITLE_KEYWORDS
    subtitle_language_tag: str = DEFAULT_SUBTITLE_LANGUAGE_TAG

    @staticmethod
    def load(path: Path) -> "AudioConfig":
        """Read the ``[audio]`` table from a corpus.toml; defaults when absent."""
        try:
            raw = _load_toml(path).get("audio", {}) or {}
        except (OSError, tomllib.TOMLDecodeError):
            raw = {}

        def as_lower_tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            value = raw.get(key)
            if value is None:
                return default
            if isinstance(value, str):
                value = [value]
            return tuple(str(v).lower() for v in value)

        return AudioConfig(
            primary_lang_codes=as_lower_tuple("primary_lang_codes", DEFAULT_PRIMARY_LANG_CODES),
            primary_title_keywords=as_lower_tuple("primary_title_keywords", DEFAULT_PRIMARY_TITLE_KEYWORDS),
            fallback_lang_codes=as_lower_tuple("fallback_lang_codes", DEFAULT_FALLBACK_LANG_CODES),
            fallback_title_keywords=as_lower_tuple("fallback_title_keywords", DEFAULT_FALLBACK_TITLE_KEYWORDS),
            subtitle_language_tag=str(raw.get("subtitle_language_tag", DEFAULT_SUBTITLE_LANGUAGE_TAG)),
        )
