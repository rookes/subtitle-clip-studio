"""Subtitle Clip Studio CLI: search subtitles, generate stitched video clips.

    clipper serve                         launch the local web UI
    clipper search "邊個" --top 10        search and print matches
    clipper generate --from last          cut the last search into one MKV

Reads a subtitle library described by config/corpus.toml (or --subtitle-root),
and writes only to the output directory (default ./clips).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audio
from .clips import DEFAULT_PAD_S, ClipRequest, generate
from .config import DEFAULT_CONFIG_DIR
from .corpus import load_episodes
from .search import Match, search

# Where `search --json` writes and `generate --from last` reads, so the two
# commands compose without a server.
LAST_SEARCH = Path(".clipper_last_search.json")


def _fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def cmd_serve(args) -> int:
    from .webapp.server import serve
    serve(args.config_dir, host=args.host, port=args.port,
          out_dir=args.out_dir, open_browser=args.open_browser)
    return 0


def cmd_search(args) -> int:
    corpus = load_episodes(
        args.config_dir, rescan=args.rescan, ai=args.ai,
        subtitle_root_override=args.subtitle_root,
        media_root_override=args.media_root,
    )
    results = search(
        corpus, args.query,
        regex=args.regex, top=args.top,
        exclude_shows=tuple(args.exclude_show), only_shows=tuple(args.only_show),
        require_media=args.require_media,
    )
    if not results:
        print("No matches.")
    for i, m in enumerate(results, 1):
        badge = "" if m.media_status == "video" else f" [{m.media_status}]"
        print(f"{i:>3}  {m.show_title}  {_fmt_time(m.start)}  {m.text}{badge}")

    payload = {
        "query": args.query,
        "media_root": str(corpus.media_root) if corpus.media_root else None,
        "results": [m.to_dict() for m in results],
    }
    out = Path(args.json) if args.json else LAST_SEARCH
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.json:
        print(f"\nWrote {len(results)} result(s) to {out}")
    return 0


def cmd_generate(args) -> int:
    src = LAST_SEARCH if args.from_ in ("last", None) else Path(args.from_)
    if not src.is_file():
        print(f"No search results at {src}. Run `clipper search` first.", file=sys.stderr)
        return 1
    payload = json.loads(src.read_text(encoding="utf-8"))
    matches = [Match.from_dict(d) for d in payload.get("results", [])]

    if args.select:
        idx = _parse_select(args.select, len(matches))
        matches = [matches[i] for i in idx]

    media_root = Path(payload["media_root"]) if payload.get("media_root") else None
    if media_root is None:
        print("No media_root recorded in the search results; nothing to cut.", file=sys.stderr)
        return 1

    items = [ClipRequest(match=m) for m in matches]
    report = generate(items, media_root, Path(args.out), pad_s=args.pad)
    print(report.summary())
    return 0 if report.output else 2


def _parse_select(spec: str, n: int) -> list[int]:
    """1-based comma list / ranges → 0-based indices, clamped to [0, n)."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return [i - 1 for i in out if 1 <= i <= n]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clipper", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                   help="config directory holding corpus.toml/media_map.toml "
                        "(default: the project's config/)")
    sub = p.add_subparsers(dest="command", required=True)

    ps = sub.add_parser("serve", help="launch the local web UI (opens a browser)")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8765)
    ps.add_argument("--out-dir", type=Path, default=Path("clips"))
    ps.add_argument("--no-open", dest="open_browser", action="store_false",
                    help="do not open a browser window automatically")
    ps.set_defaults(func=cmd_serve, open_browser=True)

    pq = sub.add_parser("search", help="search subtitles and print matches")
    pq.add_argument("query")
    pq.add_argument("--regex", action="store_true", help="treat query as a regex")
    pq.add_argument("--top", type=int, default=10)
    pq.add_argument("--only-show", action="append", default=[], metavar="NAME")
    pq.add_argument("--exclude-show", action="append", default=[], metavar="NAME")
    pq.add_argument("--require-media", action="store_true",
                    help="only matches whose linked media has a video track")
    pq.add_argument("--subtitle-root", type=Path, default=None, metavar="DIR",
                    help="point the search at this master subtitle directory "
                         "(overrides corpus.toml's root)")
    pq.add_argument("--media-root", type=Path, default=None, metavar="DIR",
                    help="override the media root used for linking (media_map.toml)")
    pq.add_argument("--ai", action="store_true", help="include AI-generated subtitles")
    pq.add_argument("--rescan", action="store_true",
                    help="rescan the corpus in memory instead of using data/manifest.json")
    pq.add_argument("--json", metavar="FILE", help="also write results to FILE")
    pq.set_defaults(func=cmd_search)

    pg = sub.add_parser("generate", help="cut a search result into one stitched MKV")
    pg.add_argument("--from", dest="from_", default="last",
                    help="'last' (default) or a JSON file from `search --json`")
    pg.add_argument("--select", help="1-based indices/ranges, e.g. 1,3,5-8")
    pg.add_argument("--pad", type=float, default=DEFAULT_PAD_S,
                    help="seconds of padding around each line (default 0.5)")
    pg.add_argument("--out", type=Path, default=Path("clips") / "clip.mkv")
    pg.set_defaults(func=cmd_generate)
    return p


def _force_utf8_output() -> None:
    """Subtitles are often non-Latin (the default corpus is Cantonese); a cp1252
    Windows console would crash on them."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def cli(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    # Apply the corpus's audio-track language preferences (defaults to Cantonese
    # then any Chinese track) so generation/muxing pick the right stream & tag.
    audio.configure_from_dir(args.config_dir)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(cli())
