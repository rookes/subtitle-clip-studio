"""Load the searchable episode set from a master subtitle directory.

The corpus rules live in ``config/corpus.toml``: which SRTs are ground truth
(``include_roots``), which to drop (``exclude_globs``, ``exclude_series``), and
how to pair each one to a media file (``config/media_map.toml``). The scanner in
``manifest`` turns a directory tree into episode records; this module drives it.

Two paths to episode records:
  * fast   — load a prior ``data/manifest.json`` cache (media_path populated).
  * rescan — walk the corpus in memory (``scan_corpus`` + ``pair_media``); used
             when there is no manifest cache, when ``--rescan`` is given, when
             the subtitle/media root is overridden, or when AI search relaxes
             the exclude globs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from .config import (
    DATA_DIR,
    DEFAULT_CONFIG_DIR,
    CorpusConfig,
    MediaMapConfig,
)
from .manifest import (
    MEDIA_EXTENSIONS,
    EpisodeRecord,
    combined_season_episode_index,
    detect_source_from_path,
    load_manifest,
    pair_media,
    scan_corpus,
)

# Media containers that actually carry a video stream. MEDIA_EXTENSIONS also
# includes audio-only formats (wav/flac/…); clip generation needs a picture
# track, so we treat those as "no video".
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m2ts"}
AUDIO_ONLY_EXTENSIONS = MEDIA_EXTENSIONS - VIDEO_EXTENSIONS

# AI-generated files that live inside the human folders are hidden by these
# globs. Enabling AI search simply drops them so the files become searchable.
# NOTE: dedicated AI subtitle *trees* are never walked (they are not in
# include_roots); adding those requires the corpus owner to supply the path.
# See load_episodes(ai=True) for where that would hook in.
_AI_EXCLUDE_GLOBS = ("*(AI-generated)*", "*AIGEN*", "*AI GEN*")


@dataclass(frozen=True)
class Corpus:
    """Everything the search/generate layers need, resolved once."""
    records: list[EpisodeRecord]      # included only (exclusion is None)
    corpus_root: Path                 # SRT paths are relative to this
    media_root: Path | None           # media_path is relative to this (may be None)

    def srt_abspath(self, rec: EpisodeRecord) -> Path:
        return self.corpus_root / rec.srt_path


def _config_paths(config_dir: Path) -> tuple[Path, Path]:
    return config_dir / "corpus.toml", config_dir / "media_map.toml"


def _scan_in_memory(corpus_cfg: CorpusConfig, media_cfg: MediaMapConfig) -> list[EpisodeRecord]:
    records = scan_corpus(corpus_cfg)
    pair_media(records, media_cfg, corpus_cfg.root)
    return records


def load_episodes(
    config_dir: Path | str = DEFAULT_CONFIG_DIR,
    *,
    rescan: bool = False,
    ai: bool = False,
    media_root_override: Path | None = None,
    subtitle_root_override: Path | None = None,
) -> Corpus:
    """Return the included episode records plus resolved corpus/media roots.

    ``rescan`` forces an in-memory walk instead of reusing ``data/manifest.json``.
    ``ai`` relaxes the AI exclude globs (in-memory walk is implied).
    ``subtitle_root_override`` replaces the corpus config's ``root`` — the "point
    the app at a master subtitle directory" user setting — so a user can index
    any subtitle library without editing the shipped config. ``media_root_override``
    replaces the config's media root (another user setting). Because scanning and
    media pairing both depend on the on-disk tree, supplying either override
    forces an in-memory walk so the result reflects the new root(s).
    """
    config_dir = Path(config_dir)
    corpus_path, media_path = _config_paths(config_dir)
    corpus_cfg = CorpusConfig.load(corpus_path)
    media_cfg = MediaMapConfig.load(media_path)
    if subtitle_root_override is not None:
        corpus_cfg = replace(corpus_cfg, root=Path(subtitle_root_override))
    if media_root_override is not None:
        media_cfg = replace(media_cfg, media_root=media_root_override)

    override = subtitle_root_override is not None or media_root_override is not None
    if ai:
        kept = tuple(g for g in corpus_cfg.exclude_globs if g not in _AI_EXCLUDE_GLOBS)
        corpus_cfg = replace(corpus_cfg, exclude_globs=kept)
        # TODO: when the AI subtitle tree location is known, also extend
        # corpus_cfg.include_roots here so those folders get walked.
        records = _scan_in_memory(corpus_cfg, media_cfg)
    elif rescan or override:
        records = _scan_in_memory(corpus_cfg, media_cfg)
    else:
        manifest_path = DATA_DIR / "manifest.json"
        if manifest_path.is_file():
            records = load_manifest(manifest_path)
        else:
            records = _scan_in_memory(corpus_cfg, media_cfg)

    included = [r for r in records if r.exclusion is None]
    return Corpus(records=included, corpus_root=corpus_cfg.root, media_root=media_cfg.media_root)


def media_abspath(rec: EpisodeRecord, media_root: Path | None) -> Path | None:
    """Absolute path to the linked media file, or None if not linked."""
    if rec.media_path is None or media_root is None:
        return None
    return media_root / rec.media_path


def has_video(rec: EpisodeRecord, media_root: Path | None) -> bool:
    """True iff the record links to an existing file with a video stream."""
    path = media_abspath(rec, media_root)
    if path is None or not path.is_file():
        return False
    return path.suffix.lower() in VIDEO_EXTENSIONS


def media_status(rec: EpisodeRecord, media_root: Path | None) -> str:
    """One-word classification used for skip reasons and the UI badge."""
    path = media_abspath(rec, media_root)
    if path is None:
        return "unlinked"
    if not path.is_file():
        return "missing"
    if path.suffix.lower() in AUDIO_ONLY_EXTENSIONS:
        return "audio_only"
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


# --- server-side file browsing (backs the web UI's "choose a file/directory") -

def list_dir(path: Path, *, videos_only: bool = False,
             exts: tuple[str, ...] | None = None) -> list[dict]:
    """List one directory's contents for the browse modal.

    Directories always come first (alphabetical), then files (alphabetical).
    ``videos_only`` keeps only ``VIDEO_EXTENSIONS``; ``exts`` (lowercase suffix
    strings like ``".srt"`` or ``".bookmarks"``) keeps only files whose name
    ends with one of them — used when picking a subtitle or bookmarks file. A
    ``..`` entry is included unless ``path`` is a filesystem root. This is a
    local, single-user tool bound to 127.0.0.1 by default, so browsing is not
    path-restricted — the same trust boundary as the existing arbitrary-file
    download route.
    """
    path = Path(path)
    entries: list[dict] = []
    parent = path.parent
    if parent != path:
        entries.append({"name": "..", "path": str(parent), "is_dir": True})

    try:
        children = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return entries

    dirs = [p for p in children if p.is_dir()]
    files = [p for p in children if p.is_file()]
    if videos_only:
        files = [p for p in files if p.suffix.lower() in VIDEO_EXTENSIONS]
    if exts:
        low = tuple(e.lower() for e in exts)
        files = [p for p in files if p.name.lower().endswith(low)]

    for p in dirs:
        entries.append({"name": p.name, "path": str(p), "is_dir": True})
    for p in files:
        entries.append({"name": p.name, "path": str(p), "is_dir": False})
    return entries


# Mirrors manifest._RE_SEASON_EPISODE / _index_media's approach (private helpers
# in that module) so a picked directory can be re-paired to episodes without
# reaching into another module's internals.
_RE_SEASON_EPISODE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")
_RE_EPISODE_ONLY = re.compile(r"(?<![A-Za-z0-9])(?:E[Pp]?\s*)?(\d{1,4})(?![A-Za-z0-9])")
_RE_NXM = re.compile(r"(\d+)x(\d+)")


def _index_videos_by_episode(directory: Path) -> dict[tuple[int | None, int], list[Path]]:
    index: dict[tuple[int | None, int], list[Path]] = {}
    for f in directory.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        season = episode = None
        m = _RE_SEASON_EPISODE.search(f.name)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
        else:
            m3 = _RE_NXM.search(f.name)
            m2 = _RE_EPISODE_ONLY.search(f.stem)
            if m3:
                season, episode = int(m3.group(1)), int(m3.group(2))
            elif m2:
                episode = int(m2.group(1))
        if episode is not None:
            index.setdefault((season, episode), []).append(f)
    return index


def match_videos_in_directory(
    items: list[dict], directory: Path
) -> dict[str, str | None]:
    """Re-pair a whole series to a user-picked directory.

    ``items`` is ``[{episode_id, season, episodes, source}]`` for the series'
    currently loaded results (``source`` is the active version, e.g. ``BD``, and
    is optional). Returns ``{episode_id: absolute_path_or_None}``; episodes with
    no confident match come back ``None`` (the caller treats that as "unlinked"
    — no error, the user picked the directory, we trust them).

    Version fuzzy-matching: when a picked directory holds several versions of an
    episode side by side (``Season 1 - BD (sync)`` / ``… - DVD (sync)`` /
    ``… - Netflix``), the file whose name/folder source matches the item's
    ``source`` wins. A plain ``S1`` / ``Season 1`` folder (no version tag) still
    matches by default, since it's the only candidate.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {it["episode_id"]: None for it in items}

    index = _index_videos_by_episode(directory)
    all_videos = [p for p in directory.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]

    result: dict[str, str | None] = {}
    # Pass 1: ordinary matching; movies pair to the lone video. Track which
    # seasons found a match so the combined-format fallback stays per-season.
    episode_items: list[dict] = []
    candidates_by_id: dict[str, list[Path]] = {}
    seasons_with_match: set[int | None] = set()
    for it in items:
        episodes = it.get("episodes") or []
        if not episodes:  # movie: only confident if the directory has exactly one video
            result[it["episode_id"]] = str(all_videos[0]) if len(all_videos) == 1 else None
            continue
        season = it.get("season")
        candidates = index.get((season, episodes[0])) or index.get((None, episodes[0])) or []
        candidates_by_id[it["episode_id"]] = candidates
        episode_items.append(it)
        if candidates:
            seasons_with_match.add(season)

    # Pass 2: combined SxxEyy-as-one-number (101.mkv -> S1E1) for a season that
    # matched nothing normally.
    combined: dict[tuple[int, int], list[Path]] | None = None
    for it in episode_items:
        season = it.get("season")
        if candidates_by_id[it["episode_id"]] or season is None \
                or season in seasons_with_match:
            continue
        if combined is None:
            combined = combined_season_episode_index(all_videos)
        hit = combined.get((season, (it.get("episodes") or [0])[0]))
        if hit:
            candidates_by_id[it["episode_id"]] = hit

    for it in episode_items:
        chosen = _pick_video(candidates_by_id[it["episode_id"]], it.get("source"))
        result[it["episode_id"]] = str(chosen) if chosen is not None else None
    return result


def _pick_video(candidates: list[Path], source: str | None) -> Path | None:
    """Choose one video for an episode from possibly several versions.

    One candidate → take it. Several → prefer the one whose filename/folder
    source matches ``source``; if that's unique, use it, otherwise fall back to
    a single version-less (generic-folder) candidate. Still ambiguous → None.
    """
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    if source:
        matched = [c for c in candidates if detect_source_from_path(c) == source]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return sorted(matched)[0]
    generic = [c for c in candidates if detect_source_from_path(c) is None]
    if len(generic) == 1:
        return generic[0]
    return None
