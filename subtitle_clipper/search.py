"""Search corpus subtitles for a string or regex and rank the top matches.

Matching is over ``Cue.text`` (the parser's cleaned, on-screen text) so users
search for what they'd read on screen. SRTs are parsed lazily and cached per
process (keyed by path + mtime) so repeated web searches stay cheap.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .manifest import EpisodeRecord, variant_rank
from .srt import Cue, SrtParseError, parse_srt

from .corpus import Corpus, media_status


@dataclass(frozen=True)
class Match:
    episode_id: str
    show_slug: str
    show_title: str
    season: int | None
    episodes: tuple[int, ...]
    cue_index: int
    start: float
    end: float
    text: str
    srt_path: str
    media_path: str | None
    media_status: str          # video | audio_only | missing | unlinked | unknown
    source: str | None = None          # release version of the active variant (BD/DVD/NF/…)
    sync_variant_key: str = ""          # source-agnostic episode identity
    # Every version of this episode that matched the same line, preferred first.
    # Each is a plain dict (see _version_dict); the top-level fields above mirror
    # versions[0]. Length 1 when only one version exists for the line.
    versions: tuple[dict, ...] = ()

    @property
    def has_video(self) -> bool:
        return self.media_status == "video"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["episodes"] = list(self.episodes)
        d["has_video"] = self.has_video
        d["versions"] = [dict(v) for v in self.versions]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Match":
        return cls(
            episode_id=d["episode_id"],
            show_slug=d["show_slug"],
            show_title=d["show_title"],
            season=d.get("season"),
            episodes=tuple(d.get("episodes") or ()),
            cue_index=int(d["cue_index"]),
            start=float(d["start"]),
            end=float(d["end"]),
            text=d["text"],
            srt_path=d["srt_path"],
            media_path=d.get("media_path"),
            media_status=d.get("media_status", "unknown"),
            source=d.get("source"),
            sync_variant_key=d.get("sync_variant_key", ""),
            versions=tuple(d.get("versions") or ()),
        )


# --- SRT cache ---------------------------------------------------------------

_cache: dict[str, tuple[float, list[Cue]]] = {}


def _cued(path: Path) -> list[Cue]:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    hit = _cache.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        cues = parse_srt(path)
    except (SrtParseError, OSError):
        cues = []
    _cache[key] = (mtime, cues)
    return cues


def clear_cache() -> None:
    _cache.clear()


# --- search ------------------------------------------------------------------

def _show_names(rec: EpisodeRecord) -> tuple[str, ...]:
    names = [rec.show_slug, rec.show_title_en]
    if rec.show_title_zh:
        names.append(rec.show_title_zh)
    return tuple(n.lower() for n in names if n)


def _show_filtered(rec: EpisodeRecord, exclude: frozenset[str], only: frozenset[str]) -> bool:
    """True if this record should be dropped by the show filters."""
    names = _show_names(rec)
    if only and not any(o in name for o in only for name in names):
        return True
    if exclude and any(x in name for x in exclude for name in names):
        return True
    return False


def _match_of(rec: EpisodeRecord, cue: Cue, status: str,
              versions: tuple[dict, ...] = ()) -> Match:
    return Match(
        episode_id=rec.episode_id,
        show_slug=rec.show_slug,
        show_title=rec.show_title_en,
        season=rec.season,
        episodes=tuple(rec.episodes),
        cue_index=cue.index,
        start=cue.start,
        end=cue.end,
        text=cue.text,
        srt_path=rec.srt_path,
        media_path=rec.media_path,
        media_status=status,
        source=rec.source,
        sync_variant_key=rec.sync_variant_key,
        versions=versions,
    )


def _version_dict(rec: EpisodeRecord, cue: Cue, status: str) -> dict:
    return {
        "source": rec.source,
        "episode_id": rec.episode_id,
        "cue_index": cue.index,
        "start": cue.start,
        "end": cue.end,
        "text": cue.text,
        "srt_path": rec.srt_path,
        "media_path": rec.media_path,
        "media_status": status,
        "season": rec.season,
        "episodes": list(rec.episodes),
        "has_video": status == "video",
    }


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _slots_for_variant(entries: list[tuple[EpisodeRecord, Cue, str]]) -> list[Match]:
    """Collapse one episode's matching cues (across its versions) into per-line
    slots. Versions are aligned by identical text and occurrence order: the k-th
    "係喎" in BD pairs with the k-th "係喎" in DVD. Each slot becomes one Match
    whose ``versions`` lists the versions that have that line, preferred first.
    """
    # Group the raw entries by version (episode_id), preserving first-seen order.
    by_version: dict[str, dict] = {}
    version_order: list[str] = []
    for rec, cue, status in entries:
        v = by_version.get(rec.episode_id)
        if v is None:
            v = {"rec": rec, "status": status, "cues": []}
            by_version[rec.episode_id] = v
            version_order.append(rec.episode_id)
        v["cues"].append(cue)

    slots: dict[tuple[str, int], dict] = {}     # (norm_text, ordinal) -> source -> (rec,cue,status)
    slot_order: list[tuple[str, int]] = []
    for eid in version_order:
        v = by_version[eid]
        v["cues"].sort(key=lambda c: c.start)
        counts: dict[str, int] = {}
        for cue in v["cues"]:
            nt = _norm(cue.text)
            ordi = counts.get(nt, 0)
            counts[nt] = ordi + 1
            key = (nt, ordi)
            if key not in slots:
                slots[key] = {}
                slot_order.append(key)
            slots[key][v["rec"].source or ""] = (v["rec"], cue, v["status"])

    matches: list[Match] = []
    for key in slot_order:
        ordered = sorted(slots[key].values(), key=lambda t: variant_rank(t[0].source))
        rep_rec, rep_cue, rep_status = ordered[0]
        version_dicts = tuple(_version_dict(r, c, s) for r, c, s in ordered)
        matches.append(_match_of(rep_rec, rep_cue, rep_status, version_dicts))
    return matches


def search(
    corpus: Corpus,
    query: str,
    *,
    regex: bool = False,
    top: int = 10,
    exclude_shows: tuple[str, ...] = (),
    only_shows: tuple[str, ...] = (),
    require_media: bool = False,
    group_versions: bool = False,
) -> list[Match]:
    """Return up to ``top`` matches in stable corpus order.

    ``require_media`` keeps only matches whose linked media has a video stream
    (missing / audio-only / unlinked are dropped).

    ``group_versions`` collapses multiple releases of the same episode (BD, DVD,
    streaming — same ``sync_variant_key``) into one result per line, with the
    other versions attached under ``Match.versions`` so the UI can switch. This
    requires scanning the whole filtered corpus (versions live in separate
    season folders), so it is off by default for the CLI's fast top-N path.
    """
    if not query:
        return []

    if regex:
        pattern = re.compile(query)
        predicate = lambda text: pattern.search(text) is not None  # noqa: E731
    else:
        needle = query.casefold()
        predicate = lambda text: needle in text.casefold()  # noqa: E731

    exclude = frozenset(s.lower() for s in exclude_shows if s)
    only = frozenset(s.lower() for s in only_shows if s)

    if not group_versions:
        results: list[Match] = []
        for rec in corpus.records:
            if _show_filtered(rec, exclude, only):
                continue
            status = media_status(rec, corpus.media_root)
            if require_media and status != "video":
                continue
            for cue in _cued(corpus.srt_abspath(rec)):
                if predicate(cue.text):
                    results.append(_match_of(rec, cue, status))
                    if len(results) >= top:
                        return results
        return results

    # Grouped: gather every matching cue by episode identity, then form slots.
    by_variant: dict[str, list[tuple[EpisodeRecord, Cue, str]]] = {}
    variant_order: list[str] = []
    for rec in corpus.records:
        if _show_filtered(rec, exclude, only):
            continue
        status = media_status(rec, corpus.media_root)
        if require_media and status != "video":
            continue
        for cue in _cued(corpus.srt_abspath(rec)):
            if not predicate(cue.text):
                continue
            key = rec.sync_variant_key
            if key not in by_variant:
                by_variant[key] = []
                variant_order.append(key)
            by_variant[key].append((rec, cue, status))

    grouped: list[Match] = []
    for key in variant_order:
        grouped.extend(_slots_for_variant(by_variant[key]))
        if len(grouped) >= top:
            break
    return grouped[:top]
