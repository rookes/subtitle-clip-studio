"""Corpus scanner: walk the subtitle directory, parse filenames, pair media.

Only the configured include_roots are walked. Every included SRT becomes an
EpisodeRecord; files that match an exclude glob or fail filename parsing are
recorded with a reason so the scan report can surface them instead of silently
dropping data.

Vendored (self-contained) from the CantoCaptions dataset tooling; the filename
conventions it understands (``Show -- 中文 (Year)/Season 1 - BD/…SxxEyy.NF.yue.srt``)
are the corpus layout this app expects, but nothing here is show-specific.
"""

import fnmatch
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CorpusConfig, MediaMapConfig

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m2ts",
                    ".wav", ".flac", ".mka", ".m4a", ".opus"}

_SOURCES = ("AMZN", "DSNP", "TVB-J2", "TVB", "ATV", "NF", "UHDBD", "BD", "DVD",
            "WEB", "YT")
_FOLDER_SOURCE_KEYWORDS = {
    "netflix": "NF", "amazon": "AMZN", "disney": "DSNP", "uhdbd": "UHDBD",
    "uhd": "UHDBD", "bd": "BD", "blu-ray": "BD", "bluray": "BD", "dvd": "DVD",
    "tvb": "TVB", "atv": "ATV", "j2": "TVB-J2", "youtube": "YT", "web": "WEB",
}
_LANG_TOKENS = {"yue", "cht", "chs", "chi", "en", "eng"}

# Preferred sync-variant order when one episode exists in several versions:
# physical masters first (UHD/BD > DVD), then streaming, then broadcast, then
# YouTube. Shared by the exporter (keeps one variant per episode) and the clip
# tool (collapses versions into one switchable result).
VARIANT_PREFERENCE = ["UHDBD", "BD", "DVD", "AMZN", "DSNP", "NF", "WEB",
                      "TVB", "TVB-J2", "ATV", "YT"]


def variant_rank(source: str | None) -> int:
    """Lower is more preferred; unknown/None sources sort last."""
    try:
        return VARIANT_PREFERENCE.index(source or "")
    except ValueError:
        return len(VARIANT_PREFERENCE)


# "Bread Barbershop" doubled-suffix bug: "...S3E01.NF.yue.chtNF.yue.cht.srt"
_RE_DOUBLED_SUFFIX = re.compile(
    r"((?:" + "|".join(_SOURCES) + r")\.(?:yue|cht|chs)\.(?:yue|cht|chs))\1+$"
)
_RE_SEASON_EPISODE = re.compile(r"[Ss](\d{1,2})[Ee][Pp]?(\d{1,4})(?:-[Ee]?[Pp]?(\d{1,4}))?")
_RE_EPISODE_ONLY = re.compile(r"(?<![A-Za-z0-9])[Ee][Pp]?(\d{1,4})(?:-[Ee]?[Pp]?(\d{1,4}))?(?!\d)")
_RE_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_RE_SHOW_FOLDER = re.compile(
    r"^(?P<en>.+?)(?:\s*--\s*(?P<zh>.+?))?\s*(?:\((?P<year>\d{4})\))?$"
)
_RE_SEASON_FOLDER = re.compile(r"[Ss]eason\s*(\d+)|(?<![A-Za-z0-9])S(\d{1,2})(?![A-Za-z0-9])")


def slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9一-鿿]+", "-", text.lower())
    return text.strip("-")


@dataclass
class EpisodeRecord:
    episode_id: str
    srt_path: str                    # relative to corpus root, forward slashes
    category: str                    # series | movies
    dub_type: str                    # dubbed | original
    show_slug: str
    show_title_en: str
    show_title_zh: str | None
    year: int | None
    season: int | None
    episodes: list[int]              # empty for movies
    source: str | None               # BD/DVD/NF/AMZN/DSNP/TVB/…
    sync_variant_key: str            # source-agnostic episode identity
    langs: list[str]
    srt_sha256: str
    media_path: str | None = None    # relative to media root
    media_flags: list[str] = field(default_factory=list)
    exclusion: str | None = None     # None = included


@dataclass
class ParsedName:
    season: int | None
    episodes: list[int]
    source: str | None
    langs: list[str]
    year: int | None
    ok: bool
    problem: str | None = None


def parse_srt_filename(filename: str, is_movie: bool) -> ParsedName:
    stem = filename[:-4] if filename.lower().endswith(".srt") else filename
    stem = _RE_DOUBLED_SUFFIX.sub(r"\1", stem)

    # Peel language tokens off the end.
    langs: list[str] = []
    tokens = stem.split(".")
    while tokens and tokens[-1].lower() in _LANG_TOKENS:
        langs.insert(0, tokens[-1].lower())
        tokens.pop()

    source = None
    for tok in reversed(tokens):
        if tok.upper() in _SOURCES:
            source = tok.upper()
            break

    body = ".".join(tokens)
    year_m = _RE_YEAR.search(body)
    year = int(year_m.group(1)) if year_m else None

    if is_movie:
        return ParsedName(None, [], source, langs, year, ok=True)

    m = _RE_SEASON_EPISODE.search(body)
    if m:
        season = int(m.group(1))
        eps = [int(m.group(2))]
        if m.group(3):
            eps = list(range(int(m.group(2)), int(m.group(3)) + 1))
        return ParsedName(season, eps, source, langs, year, ok=True)

    m = _RE_EPISODE_ONLY.search(body)
    if m:
        eps = [int(m.group(1))]
        if m.group(2):
            eps = list(range(int(m.group(1)), int(m.group(2)) + 1))
        return ParsedName(None, eps, source, langs, year, ok=True)

    return ParsedName(None, [], source, langs, year, ok=False,
                      problem="no episode number found")


def parse_show_folder(name: str) -> tuple[str, str | None, int | None]:
    m = _RE_SHOW_FOLDER.match(name.strip())
    en = m.group("en").strip().rstrip("-").strip()
    zh = m.group("zh").strip() if m.group("zh") else None
    year = int(m.group("year")) if m.group("year") else None
    return en, zh, year


def parse_season_folder(name: str) -> tuple[int | None, str | None]:
    season = None
    m = _RE_SEASON_FOLDER.search(name)
    if m:
        season = int(m.group(1) or m.group(2))
    return season, _source_from_folder_name(name)


def _source_from_folder_name(name: str) -> str | None:
    lowered = name.lower()
    for keyword, src in _FOLDER_SOURCE_KEYWORDS.items():
        if keyword in lowered:
            return src
    return None


_RE_NAME_TOKENS = re.compile(r"[.\s_\-()\[\]]+")


def detect_source_from_path(path: Path) -> str | None:
    """Best-effort release source (BD/DVD/NF/…) for a media file.

    Filename source tokens win (most specific); otherwise the first containing
    folder whose name carries a source keyword (``Season 1 - BD (sync)`` →
    ``BD``). Used to disambiguate several candidate files for one episode by
    matching the subtitle's own version, and shared with the clip tool so both
    pair multi-version shows the same way.
    """
    for tok in _RE_NAME_TOKENS.split(path.stem):
        if tok.upper() in _SOURCES:
            return tok.upper()
    for part in reversed(path.parts[:-1]):
        src = _source_from_folder_name(part)
        if src:
            return src
    return None


def _episode_label(season: int | None, episodes: list[int]) -> str:
    if not episodes:
        return "movie"
    s = f"s{season:02d}" if season is not None else "s01"
    if len(episodes) == 1:
        return f"{s}e{episodes[0]:04d}" if episodes[0] > 99 else f"{s}e{episodes[0]:02d}"
    width = 4 if episodes[-1] > 99 else 2
    return f"{s}e{episodes[0]:0{width}d}-{episodes[-1]:0{width}d}"


def scan_corpus(cfg: CorpusConfig) -> list[EpisodeRecord]:
    records: list[EpisodeRecord] = []
    for include_root in cfg.include_roots:
        root_dir = cfg.root / include_root
        if not root_dir.is_dir():
            continue
        category = include_root.split("/")[0].lower()      # series | movies
        dub_type = include_root.split("/")[1].split(" ")[0].lower()
        for srt_path in sorted(root_dir.rglob("*.srt")):
            rel = srt_path.relative_to(cfg.root).as_posix()
            records.append(
                _build_record(cfg, srt_path, rel, category, dub_type, root_dir)
            )
    _disambiguate_collisions(records)
    return records


def _disambiguate_collisions(records: list[EpisodeRecord]) -> None:
    """Distinct files must never share an episode_id (artifacts are keyed on
    it). Collisions are real corpus situations — two movies in one folder
    (A Chinese Odyssey Part I/II), two releases with the same source tag
    (Election, two different BD masters) — so suffix -v2/-v3 deterministically
    by sorted path and flag them for review."""
    by_id: dict[str, list[EpisodeRecord]] = {}
    for rec in records:
        if rec.exclusion is None:
            by_id.setdefault(rec.episode_id, []).append(rec)
    for episode_id, recs in by_id.items():
        if len(recs) == 1:
            continue
        for n, rec in enumerate(sorted(recs, key=lambda r: r.srt_path)):
            rec.media_flags.append("id_collision")
            if n > 0:
                rec.episode_id = f"{episode_id}-v{n + 1}"


def _build_record(cfg: CorpusConfig, srt_path: Path, rel: str, category: str,
                  dub_type: str, root_dir: Path) -> EpisodeRecord:
    exclusion = next(
        (g for g in cfg.exclude_globs if fnmatch.fnmatch(rel, g)), None
    )
    if exclusion is None:
        exclusion = next(
            (f"series_excluded: {s.name}" for s in cfg.exclude_series
             if s.matches(rel)),
            None,
        )

    rel_to_include = srt_path.relative_to(root_dir)
    show_folder = rel_to_include.parts[0] if len(rel_to_include.parts) > 1 else None
    if show_folder:
        title_en, title_zh, folder_year = parse_show_folder(show_folder)
    else:
        title_en, title_zh, folder_year = srt_path.stem, None, None

    season_folder = (
        rel_to_include.parts[-2] if len(rel_to_include.parts) > 2 else None
    )
    folder_season, folder_source = (
        parse_season_folder(season_folder) if season_folder else (None, None)
    )

    parsed = parse_srt_filename(srt_path.name, is_movie=(category == "movies"))
    season = parsed.season if parsed.season is not None else folder_season
    source = parsed.source or folder_source
    year = folder_year or parsed.year

    show_slug = slugify(title_en) + (f"-{year}" if year else "")
    label = _episode_label(season, parsed.episodes)
    source_part = (source or "unk").lower().replace("-", "")
    episode_id = f"{show_slug}_{label}_{source_part}"
    if category == "movies" or not parsed.episodes:
        # Disambiguate movies that would otherwise collide on the show folder.
        episode_id = f"{show_slug}_{slugify(srt_path.stem)[:40]}" if not show_folder \
            else f"{show_slug}_{label}_{source_part}"

    if exclusion is None and not parsed.ok and category != "movies":
        exclusion = f"unparsed_filename: {parsed.problem}"

    sha = hashlib.sha256(srt_path.read_bytes()).hexdigest() if exclusion is None else ""

    return EpisodeRecord(
        episode_id=episode_id,
        srt_path=rel,
        category=category,
        dub_type=dub_type,
        show_slug=show_slug,
        show_title_en=title_en,
        show_title_zh=title_zh,
        year=year,
        season=season,
        episodes=parsed.episodes,
        source=source,
        sync_variant_key=f"{show_slug}_{label}",
        langs=parsed.langs,
        srt_sha256=sha,
        exclusion=exclusion,
    )


# --- media pairing -----------------------------------------------------------

def pair_media(records: list[EpisodeRecord], media_map: MediaMapConfig,
               corpus_root: Path) -> None:
    """Attach media_path to included records in place. The media root mirrors
    the subtitle tree unless a per-show override says otherwise."""
    if media_map.media_root is None or not media_map.media_root.is_dir():
        return
    root = media_map.media_root

    by_show: dict[str, list[EpisodeRecord]] = {}
    for rec in records:
        if rec.exclusion is None:
            by_show.setdefault(rec.show_slug, []).append(rec)

    for show_slug, recs in by_show.items():
        override = media_map.shows.get(show_slug, {})
        show_rel = Path(recs[0].srt_path).parts[:3]  # Category/DubType/Show
        show_dir = root / override["dir"] if "dir" in override \
            else root.joinpath(*show_rel)

        if "file" in override:
            f = show_dir / override["file"] if not Path(override["file"]).is_absolute() \
                else Path(override["file"])
            for rec in recs:
                if f.is_file():
                    rec.media_path = f.relative_to(root).as_posix() \
                        if f.is_relative_to(root) else str(f)
                else:
                    rec.media_flags.append("media_missing")
            continue

        if not show_dir.is_dir():
            for rec in recs:
                rec.media_flags.append("show_dir_missing")
            continue

        media_files = [p for p in show_dir.rglob("*") if p.suffix.lower() in MEDIA_EXTENSIONS]
        pattern = re.compile(override["pattern"]) if "pattern" in override else None
        index = _index_media(media_files, pattern)
        wanted_variant = override.get("variant")

        # Pass 1: ordinary matching. Movies pair to the lone file in the folder;
        # episodes look up (season, ep) then (None, ep). Track which seasons found
        # any match so the combined-format fallback stays a per-season last resort.
        episode_recs: list[EpisodeRecord] = []
        candidates_by_rec: dict[int, list[Path]] = {}
        seasons_with_match: set[int | None] = set()
        for rec in recs:
            if not rec.episodes:  # movie in a folder without explicit file
                if len(media_files) == 1:
                    rec.media_path = media_files[0].relative_to(root).as_posix()
                else:
                    rec.media_flags.append("ambiguous_movie_media")
                continue
            if wanted_variant and rec.source and wanted_variant != rec.source:
                rec.media_flags.append("variant_mismatch")
            candidates = index.get((rec.season, rec.episodes[0])) \
                or index.get((None, rec.episodes[0])) or []
            candidates_by_rec[id(rec)] = candidates
            episode_recs.append(rec)
            if candidates:
                seasons_with_match.add(rec.season)

        # Pass 2: for a season that matched nothing normally, try the combined
        # SxxEyy-as-one-number reading (101.mkv -> S1E1).
        combined: dict[tuple[int, int], list[Path]] | None = None
        for rec in episode_recs:
            if candidates_by_rec[id(rec)] or rec.season is None \
                    or rec.season in seasons_with_match:
                continue
            if combined is None:
                combined = combined_season_episode_index(media_files)
            hit = combined.get((rec.season, rec.episodes[0]))
            if hit:
                candidates_by_rec[id(rec)] = hit

        # Pass 3: assign, disambiguating multi-version folders by source.
        for rec in episode_recs:
            candidates = candidates_by_rec[id(rec)]
            if len(candidates) == 1:
                rec.media_path = candidates[0].relative_to(root).as_posix()
            elif not candidates:
                rec.media_flags.append("media_missing")
            else:
                # Several files for this episode (a multi-version show with
                # BD/DVD/streaming folders side by side). Pin to the file whose
                # name/folder source matches this subtitle's own version.
                chosen = _pick_by_source(candidates, wanted_variant or rec.source)
                if chosen is not None:
                    rec.media_path = chosen.relative_to(root).as_posix()
                else:
                    rec.media_flags.append("ambiguous_media")


def _pick_by_source(candidates: list[Path], source: str | None) -> Path | None:
    """From several candidate files, return the one matching ``source`` (by
    filename/folder version tag), or None if that's still ambiguous."""
    if not source:
        return None
    matched = [c for c in candidates if detect_source_from_path(c) == source]
    return matched[0] if len(matched) == 1 else None


# Media-filename episode parsing. Shared with the clip tool so both pair the
# corpus the same way. _RE_MEDIA_BARE matches a lone episode number (optionally
# prefixed with E/EP); _RE_COMBINED_CODE is the stricter "season+episode fused
# into one 3-digit number" form (see combined_season_episode_index).
_RE_MEDIA_NXM = re.compile(r"(\d+)x(\d+)")
_RE_MEDIA_BARE = re.compile(r"(?<![A-Za-z0-9])(?:E[Pp]?\s*)?(\d{1,4})(?![A-Za-z0-9])")
_RE_COMBINED_CODE = re.compile(r"(?<![A-Za-z0-9])(\d{3})(?![A-Za-z0-9])")


def _index_media(files: list[Path], pattern: "re.Pattern[str] | None"
                 ) -> dict[tuple[int | None, int], list[Path]]:
    index: dict[tuple[int | None, int], list[Path]] = {}
    for f in files:
        season = episode = None
        if pattern:
            m = pattern.search(f.name)
            if m:
                gd = m.groupdict()
                season = int(gd["s"]) if gd.get("s") else None
                episode = int(gd["e"]) if gd.get("e") else None
        if episode is None:
            m = _RE_SEASON_EPISODE.search(f.name)
            if m:
                season, episode = int(m.group(1)), int(m.group(2))
            else:
                m2 = _RE_MEDIA_BARE.search(f.stem)
                m3 = _RE_MEDIA_NXM.search(f.name)
                if m3:
                    season, episode = int(m3.group(1)), int(m3.group(2))
                elif m2:
                    episode = int(m2.group(1))
        if episode is not None:
            index.setdefault((season, episode), []).append(f)
    return index


def combined_season_episode_index(
    files: list[Path],
) -> dict[tuple[int, int], list[Path]]:
    """Map (season, episode) -> files for names that fuse season and episode into
    one number (Avatar's ``101.mkv`` -> S1E1, ``213`` -> S2E13).

    A standalone 3-digit code with a nonzero leading digit is read as
    ``season = first digit`` + ``episode = last two digits``. The whole folder is
    skipped (returns ``{}``) when any such code is zero-padded (``001.mkv``): that
    signals plain sequential 3-digit episode numbering, so the leading digit is
    NOT a season. Files carrying an explicit ``SxxEyy`` / ``NxM`` tag, or an
    ``E``/``EP`` episode prefix, are never treated as combined.

    This is a *fallback*: callers apply it only for a season whose episodes got
    no ordinary media match, so it can never override a real ``SxxEyy`` pairing.
    """
    codes: list[tuple[Path, str]] = []
    for f in files:
        if _RE_SEASON_EPISODE.search(f.name) or _RE_MEDIA_NXM.search(f.name):
            continue
        m = _RE_COMBINED_CODE.search(f.stem)
        if m:
            codes.append((f, m.group(1)))
    if any(tok[0] == "0" for _, tok in codes):
        return {}
    index: dict[tuple[int, int], list[Path]] = {}
    for f, tok in codes:
        index.setdefault((int(tok[0]), int(tok[1:])), []).append(f)
    return index


# --- persistence -------------------------------------------------------------

def save_manifest(records: list[EpisodeRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "records": [asdict(r) for r in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def load_manifest(path: Path) -> list[EpisodeRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [EpisodeRecord(**r) for r in payload["records"]]
