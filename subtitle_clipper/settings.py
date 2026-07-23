"""Per-user Subtitle Clip Studio settings.

Stored as TOML *outside* the project so the app never mutates the tracked
config in ``config/``. A ``[media]`` and an ``[output]`` table:

    [media]
    subtitle_root = "D:/Subtitles"   # overrides corpus.toml's root
    media_root = "D:/Media"          # overrides media_map.toml's media_root

    [output]
    resolution = "720p"            # 480p | 720p | 1080p
    quality = "medium"             # low | medium | high

Location: ``%APPDATA%/subtitle-clip-studio/clipper.toml`` on Windows, else
``$XDG_CONFIG_HOME/subtitle-clip-studio/clipper.toml`` (falling back to
``~/.config/...``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Output resolution -> (width, height). Every generated segment is normalized to
# these dimensions (letterboxed) so the concat demuxer can stitch heterogeneous
# sources. Preview always uses its own small size, independent of this.
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}
# Quality -> libx264 CRF (lower = better/larger). "medium" keeps the previous
# fixed default so existing behavior is unchanged out of the box.
QUALITY_CRF: dict[str, int] = {"low": 26, "medium": 20, "high": 17}

DEFAULT_RESOLUTION = "720p"
DEFAULT_QUALITY = "medium"


@dataclass(frozen=True)
class ClipperSettings:
    subtitle_root: str | None = None       # override for corpus.toml's root
    media_root: str | None = None          # override for the media_map media root
    resolution: str = DEFAULT_RESOLUTION
    quality: str = DEFAULT_QUALITY

    def subtitle_root_path(self) -> Path | None:
        return Path(self.subtitle_root).expanduser() if self.subtitle_root else None

    def media_root_path(self) -> Path | None:
        return Path(self.media_root).expanduser() if self.media_root else None

    def dimensions(self) -> tuple[int, int]:
        return RESOLUTIONS.get(self.resolution, RESOLUTIONS[DEFAULT_RESOLUTION])

    def crf(self) -> int:
        return QUALITY_CRF.get(self.quality, QUALITY_CRF[DEFAULT_QUALITY])


def settings_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "subtitle-clip-studio" / "clipper.toml"


def _clean(value: str, allowed: dict, default: str) -> str:
    return value if value in allowed else default


def load_settings(path: Path | None = None) -> ClipperSettings:
    """Read settings, tolerating a missing or malformed file (returns defaults).

    Unknown resolution/quality values fall back to the defaults so generation
    can never be handed an invalid ffmpeg parameter.
    """
    path = path or settings_path()
    if not path.is_file():
        return ClipperSettings()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ClipperSettings()
    media = raw.get("media", {}) or {}
    output = raw.get("output", {}) or {}
    sub_root = (media.get("subtitle_root") or "").strip() or None
    root = (media.get("media_root") or "").strip() or None
    return ClipperSettings(
        subtitle_root=sub_root,
        media_root=root,
        resolution=_clean(str(output.get("resolution", DEFAULT_RESOLUTION)),
                          RESOLUTIONS, DEFAULT_RESOLUTION),
        quality=_clean(str(output.get("quality", DEFAULT_QUALITY)),
                       QUALITY_CRF, DEFAULT_QUALITY),
    )


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dump_toml(s: ClipperSettings) -> str:
    sub_root_line = (f"subtitle_root = {_toml_str(s.subtitle_root)}" if s.subtitle_root
                     else 'subtitle_root = ""   # empty = use corpus.toml root')
    root_line = (f"media_root = {_toml_str(s.media_root)}" if s.media_root
                 else 'media_root = ""   # empty = use the media_map.toml media_root')
    return "\n".join([
        "# Subtitle Clip Studio user settings (auto-managed by the app).",
        "",
        "[media]",
        sub_root_line,
        root_line,
        "",
        "[output]",
        f"resolution = {_toml_str(s.resolution)}   # 480p | 720p | 1080p",
        f"quality = {_toml_str(s.quality)}       # low | medium | high",
        "",
    ])


def save_settings(settings: ClipperSettings, path: Path | None = None) -> Path:
    """Write settings to disk (creating the parent dir). Returns the path."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_toml(settings), encoding="utf-8")
    return path
