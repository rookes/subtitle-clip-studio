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
    display_name: str | None = None     # filename label (custom datasets), or None
    # Every version of this episode that matched the same line, preferred first.
    # Each is a plain dict (see _version_dict); the top-level fields above mirror
    # versions[0]. Length 1 when only one version exists for the line.
    versions: tuple[dict, ...] = ()
    # A merged run of bookmarked lines (see datasets.group_bookmark_indices) is
    # ONE list entry spanning several cues — the cues stay separate, exactly as
    # when a user widens a window to pull neighbouring lines in. The fields above
    # describe the entry's FIRST line; these describe the run: ``group_end`` is
    # the last line's end and ``group_lines`` every line's text (for display).
    # Empty/None for an ordinary single-line result.
    group_end: float | None = None
    group_lines: tuple[str, ...] = ()

    @property
    def has_video(self) -> bool:
        return self.media_status == "video"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["episodes"] = list(self.episodes)
        d["has_video"] = self.has_video
        d["versions"] = [dict(v) for v in self.versions]
        d["group_lines"] = list(self.group_lines)
        return d

    # note: display_name is read from the dict in from_dict below

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
            display_name=d.get("display_name"),
            versions=tuple(d.get("versions") or ()),
            group_end=d.get("group_end"),
            group_lines=tuple(d.get("group_lines") or ()),
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


@dataclass(frozen=True)
class _Entry:
    """One searchable list entry: a cue, plus the merged run it anchors (if any).

    ``cue`` is the entry's first line — the one whose timing/text the Match
    reports. ``group`` is None for an ordinary result, or every cue of the run
    (the first one included) when several bookmarked lines share one entry. The
    cues are never fused: the run is described by its span and its lines.
    """
    rec: EpisodeRecord
    cue: Cue
    status: str
    group: tuple[Cue, ...] | None = None

    @property
    def group_end(self) -> float | None:
        return self.group[-1].end if self.group else None

    @property
    def group_lines(self) -> tuple[str, ...]:
        return tuple(c.text for c in self.group) if self.group else ()


def _entries(rec: EpisodeRecord, cues: list[Cue], status: str, predicate
             ) -> list[_Entry]:
    """The record's entries that satisfy ``predicate``, in file order.

    Normally one entry per matching cue, restricted to ``rec.cue_ids`` when set.
    A record with ``cue_groups`` (merged SubtitleEdit bookmarks) instead yields
    one entry per group, kept when ANY of its lines matches, so a merged run
    stays a single list item however the query hits it.
    """
    if rec.cue_groups:
        by_index = {c.index: c for c in cues}
        out: list[_Entry] = []
        for group in rec.cue_groups:
            members = tuple(by_index[i] for i in group if i in by_index)
            if not members or not any(predicate(c.text) for c in members):
                continue
            out.append(_Entry(rec, members[0], status,
                              group=members if len(members) > 1 else None))
        return out
    allowed = set(rec.cue_ids) if rec.cue_ids is not None else None
    return [_Entry(rec, c, status) for c in cues
            if (allowed is None or c.index in allowed) and predicate(c.text)]


def _match_of(entry: _Entry, versions: tuple[dict, ...] = ()) -> Match:
    rec, cue = entry.rec, entry.cue
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
        media_status=entry.status,
        source=rec.source,
        sync_variant_key=rec.sync_variant_key,
        display_name=rec.display_name,
        versions=versions,
        group_end=entry.group_end,
        group_lines=entry.group_lines,
    )


def _version_dict(entry: _Entry) -> dict:
    rec, cue = entry.rec, entry.cue
    return {
        "source": rec.source,
        "episode_id": rec.episode_id,
        "cue_index": cue.index,
        "start": cue.start,
        "end": cue.end,
        "text": cue.text,
        "srt_path": rec.srt_path,
        "media_path": rec.media_path,
        "media_status": entry.status,
        "season": rec.season,
        "episodes": list(rec.episodes),
        "has_video": entry.status == "video",
        "display_name": rec.display_name,
        "group_end": entry.group_end,
        "group_lines": list(entry.group_lines),
    }


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _slots_for_variant(entries: list[_Entry]) -> list[Match]:
    """Collapse one episode's matching entries (across its versions) into
    per-line slots. Versions are aligned by identical text and occurrence order:
    the k-th "係喎" in BD pairs with the k-th "係喎" in DVD. Each slot becomes one
    Match whose ``versions`` lists the versions that have that line, preferred
    first. A merged bookmark entry aligns on its whole text, so it stays one slot.
    """
    # Group the raw entries by version (episode_id), preserving first-seen order.
    by_version: dict[str, list[_Entry]] = {}
    version_order: list[str] = []
    for entry in entries:
        eid = entry.rec.episode_id
        if eid not in by_version:
            by_version[eid] = []
            version_order.append(eid)
        by_version[eid].append(entry)

    slots: dict[tuple[str, int], dict[str, _Entry]] = {}   # (norm_text, ordinal) -> source -> entry
    slot_order: list[tuple[str, int]] = []
    for eid in version_order:
        version_entries = sorted(by_version[eid], key=lambda e: e.cue.start)
        counts: dict[str, int] = {}
        for entry in version_entries:
            nt = _norm("\n".join(entry.group_lines) or entry.cue.text)
            ordi = counts.get(nt, 0)
            counts[nt] = ordi + 1
            key = (nt, ordi)
            if key not in slots:
                slots[key] = {}
                slot_order.append(key)
            slots[key][entry.rec.source or ""] = entry

    matches: list[Match] = []
    for key in slot_order:
        ordered = sorted(slots[key].values(), key=lambda e: variant_rank(e.rec.source))
        matches.append(_match_of(ordered[0], tuple(_version_dict(e) for e in ordered)))
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
    allow_empty: bool = False,
) -> list[Match]:
    """Return up to ``top`` matches in stable corpus order.

    ``allow_empty`` makes an empty ``query`` match every cue (used by custom
    datasets so an empty search lists the whole loaded source). A record whose
    ``cue_ids`` is set restricts matching to those 0-based cue indices (the
    SubtitleEdit-bookmarks case, where only bookmarked lines are searchable);
    one whose ``cue_groups`` is set additionally returns each merged run of
    bookmarked lines as a single result (see :func:`_entries`).

    ``require_media`` keeps only matches whose linked media has a video stream
    (missing / audio-only / unlinked are dropped).

    ``group_versions`` collapses multiple releases of the same episode (BD, DVD,
    streaming — same ``sync_variant_key``) into one result per line, with the
    other versions attached under ``Match.versions`` so the UI can switch. This
    requires scanning the whole filtered corpus (versions live in separate
    season folders), so it is off by default for the CLI's fast top-N path.
    """
    if not query:
        if not allow_empty:
            return []
        predicate = lambda text: True  # noqa: E731  (list the whole dataset)
    elif regex:
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
            for entry in _entries(rec, _cued(corpus.srt_abspath(rec)), status,
                                  predicate):
                results.append(_match_of(entry))
                if len(results) >= top:
                    return results
        return results

    # Grouped: gather every matching entry by episode identity, then form slots.
    by_variant: dict[str, list[_Entry]] = {}
    variant_order: list[str] = []
    for rec in corpus.records:
        if _show_filtered(rec, exclude, only):
            continue
        status = media_status(rec, corpus.media_root)
        if require_media and status != "video":
            continue
        for entry in _entries(rec, _cued(corpus.srt_abspath(rec)), status, predicate):
            key = rec.sync_variant_key
            if key not in by_variant:
                by_variant[key] = []
                variant_order.append(key)
            by_variant[key].append(entry)

    grouped: list[Match] = []
    for key in variant_order:
        grouped.extend(_slots_for_variant(by_variant[key]))
        if len(grouped) >= top:
            break
    return grouped[:top]
