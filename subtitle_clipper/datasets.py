"""Build an in-memory :class:`~subtitle_clipper.corpus.Corpus` from an ad-hoc
source the user picks in the UI, instead of the master subtitle tree.

Three sources are supported, each returning a ``(Corpus, label)`` pair the web
server installs as a temporary override:

  * a single ``.srt`` file — every cue is searchable; rows show the filename.
  * a directory of ``.srt`` files (non-recursive) — each file is cross-referenced
    against the master corpus so recognised episodes keep their show name / season
    / episode (and linked media); unrecognised files fall back to the filename.
  * a SubtitleEdit ``.SE.bookmarks`` file — only the bookmarked lines of its
    paired SRT are loaded.

Everything downstream (search / preview / generate) is unchanged: these just
produce ordinary ``EpisodeRecord`` objects, with the two custom-only fields
``display_name`` and ``cue_ids`` (see :class:`EpisodeRecord`) doing the extra work.
"""

from __future__ import annotations

import json
from pathlib import Path

from .corpus import Corpus
from .manifest import (
    EpisodeRecord,
    _episode_label,
    parse_srt_filename,
    slugify,
)
from .srt import SrtParseError, parse_srt

BOOKMARKS_SUFFIX = ".SE.bookmarks"


class DatasetError(ValueError):
    """A custom dataset could not be built (bad path, missing SRT, parse error)."""


# --- record construction -----------------------------------------------------

def _base_record(srt_name: str, srt_path: str, episode_id: str) -> EpisodeRecord:
    """A minimal record that displays the filename (no show/episode metadata)."""
    return EpisodeRecord(
        episode_id=episode_id,
        srt_path=srt_path,
        category="custom",
        dub_type="original",
        show_slug=slugify(Path(srt_name).stem) or "custom",
        show_title_en=srt_name,
        show_title_zh=None,
        year=None,
        season=None,
        episodes=[],
        source=None,
        sync_variant_key=episode_id,
        langs=[],
        srt_sha256="",
        display_name=srt_name,
    )


def _match_master(srt_name: str, master: Corpus | None) -> EpisodeRecord | None:
    """Return the single master record this filename confidently refers to, else None.

    A match requires the file to parse to an episode number *and* a master record
    whose show title (English or Chinese) appears in the filename with the same
    season + first episode. Ambiguous (>1) or absent matches return None.
    """
    if master is None or not master.records:
        return None
    parsed = parse_srt_filename(srt_name, is_movie=False)
    if not parsed.ok or not parsed.episodes:
        return None
    name_low = srt_name.lower()
    ep0 = parsed.episodes[0]
    hits: list[EpisodeRecord] = []
    for rec in master.records:
        if ep0 not in (rec.episodes or []):
            continue
        if parsed.season is not None and rec.season is not None \
                and parsed.season != rec.season:
            continue
        titles = [t for t in (rec.show_title_en, rec.show_title_zh) if t]
        if any(t.lower() in name_low for t in titles):
            hits.append(rec)
    return hits[0] if len(hits) == 1 else None


def _episode_record(srt_name: str, srt_path: str, episode_id: str,
                    master: Corpus | None) -> EpisodeRecord:
    """A record for a directory/bookmarks file: reuse master metadata + media when
    the filename maps to a known episode, else fall back to the filename label."""
    hit = _match_master(srt_name, master)
    if hit is None:
        return _base_record(srt_name, srt_path, episode_id)
    return EpisodeRecord(
        episode_id=episode_id,
        srt_path=srt_path,
        category="custom",
        dub_type=hit.dub_type,
        show_slug=hit.show_slug,
        show_title_en=hit.show_title_en,
        show_title_zh=hit.show_title_zh,
        year=hit.year,
        season=hit.season,
        episodes=list(hit.episodes),
        source=hit.source,
        sync_variant_key=hit.sync_variant_key,
        langs=list(hit.langs),
        srt_sha256="",
        media_path=hit.media_path,   # relative to master.media_root (reused below)
    )


# --- the three loaders -------------------------------------------------------

def load_single_srt(path: Path | str) -> tuple[Corpus, str]:
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"Not a file: {path}")
    _ensure_parseable(path)
    rec = _base_record(path.name, path.name, f"custom_{slugify(path.stem)}")
    corpus = Corpus(records=[rec], corpus_root=path.parent, media_root=None)
    return corpus, path.name


def load_directory(path: Path | str, master: Corpus | None) -> tuple[Corpus, str]:
    path = Path(path)
    if not path.is_dir():
        raise DatasetError(f"Not a directory: {path}")
    srts = sorted(p for p in path.glob("*.srt") if p.is_file())
    if not srts:
        raise DatasetError(f"No .srt files in {path}")
    records: list[EpisodeRecord] = []
    for i, srt in enumerate(srts):
        eid = f"custom_{i}_{slugify(srt.stem)}"
        records.append(_episode_record(srt.name, srt.name, eid, master))
    media_root = master.media_root if master is not None else None
    corpus = Corpus(records=records, corpus_root=path, media_root=media_root)
    return corpus, f"{path.name} ({len(records)} file(s))"


def load_bookmarks(path: Path | str, master: Corpus | None) -> tuple[Corpus, str]:
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"Not a file: {path}")
    name = path.name
    if not name.lower().endswith(BOOKMARKS_SUFFIX.lower()):
        raise DatasetError(f"Not a SubtitleEdit bookmarks file: {name}")
    srt_path = path.with_name(name[: -len(BOOKMARKS_SUFFIX)])
    if not srt_path.is_file():
        raise DatasetError(
            f"Paired SRT not found next to the bookmarks file: {srt_path.name}"
        )

    idxs = _read_bookmark_indices(path)
    cues = _ensure_parseable(srt_path)
    by_number = {c.number: c.index for c in cues if c.number is not None}
    cue_ids: list[int] = []
    for idx in idxs:
        if idx in by_number:
            cue_ids.append(by_number[idx])
        elif 1 <= idx <= len(cues):        # fallback: idx as 1-based position
            cue_ids.append(cues[idx - 1].index)
    cue_ids = sorted(set(cue_ids))

    eid = f"custom_bm_{slugify(srt_path.stem)}"
    rec = _episode_record(srt_path.name, srt_path.name, eid, master)
    rec.cue_ids = cue_ids
    media_root = master.media_root if master is not None else None
    corpus = Corpus(records=[rec], corpus_root=srt_path.parent, media_root=media_root)
    return corpus, f"{name} ({len(cue_ids)} bookmark(s))"


# --- helpers -----------------------------------------------------------------

def _read_bookmark_indices(path: Path) -> list[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise DatasetError(f"Could not read bookmarks file: {e}") from e
    marks = data.get("bookmarks") if isinstance(data, dict) else None
    if not isinstance(marks, list):
        raise DatasetError("Bookmarks file has no 'bookmarks' list")
    out: list[int] = []
    for m in marks:
        if isinstance(m, dict) and isinstance(m.get("idx"), int):
            out.append(m["idx"])
    if not out:
        raise DatasetError("Bookmarks file contains no entries")
    return out


def _ensure_parseable(path: Path):
    try:
        return parse_srt(path)
    except (SrtParseError, OSError) as e:
        raise DatasetError(f"Could not parse SRT {path.name}: {e}") from e
