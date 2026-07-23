"""Audio-track probing and language-aware track selection for clip generation.

Self-contained ffprobe helpers (no heavy ML deps): given a media file, list its
audio streams and pick the one whose language the user prefers. The preference
is configurable via the ``[audio]`` table in corpus.toml (see AudioConfig) and
defaults to Cantonese first, then any Chinese track, then ffmpeg's default.

A single process-wide :class:`AudioConfig` is held here (initialised to the
Cantonese/Chinese defaults). The CLI and web server call :func:`configure_from_dir`
once at startup so a customised corpus.toml localises track selection.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import AudioConfig

# Process-wide current config; defaults to Cantonese-first behaviour until the
# CLI/server loads the corpus config over it.
_config = AudioConfig()


def configure(config: AudioConfig) -> None:
    """Install the language preferences used by later track selection / muxing."""
    global _config
    _config = config


def configure_from_dir(config_dir: Path | str) -> None:
    """Load the ``[audio]`` table from ``<config_dir>/corpus.toml`` and apply it.

    Missing file or table falls back to the built-in Cantonese/Chinese defaults.
    """
    path = Path(config_dir) / "corpus.toml"
    configure(AudioConfig.load(path))


def current() -> AudioConfig:
    return _config


def subtitle_language_tag() -> str:
    """Language tag written onto muxed subtitle tracks (e.g. ``yue``)."""
    return _config.subtitle_language_tag


def probe_audio_tracks(file: str) -> list[dict]:
    """Return ffprobe metadata for all audio streams in *file*.

    Empty list if the file has no audio streams or ffprobe cannot read it.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json", "-show_streams",
        "-select_streams", "a", file,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False)
        return json.loads(result.stdout).get("streams", [])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return []


def _matches(stream: dict, lang_codes: tuple[str, ...],
             title_keywords: tuple[str, ...]) -> bool:
    tags = stream.get("tags", {})
    if (tags.get("language") or "").lower() in lang_codes:
        return True
    title = (tags.get("title") or "").lower()
    return any(k in title for k in title_keywords)


def select_track(streams: list[dict], config: AudioConfig | None = None) -> int:
    """Return the 0-based audio stream index for the preferred track.

    Preference order (all configurable, Cantonese/Chinese by default):
      1. a primary-language track (e.g. an explicit Cantonese stream);
      2. otherwise the first fallback-language track (e.g. any Chinese stream)
         so a clear match is used instead of ffmpeg's default (often an English
         or Japanese dub);
      3. failing that, 0 — let ffmpeg choose.
    """
    cfg = config or _config
    for i, st in enumerate(streams):
        if _matches(st, cfg.primary_lang_codes, cfg.primary_title_keywords):
            return i
    for i, st in enumerate(streams):
        if _matches(st, cfg.fallback_lang_codes, cfg.fallback_title_keywords):
            return i
    return 0


def select_audio_track(src: Path) -> int:
    """Probe ``src`` and return the preferred audio stream index (0 on failure)."""
    return select_track(probe_audio_tracks(str(src)))
