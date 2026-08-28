"""Flask app: search JSON API, on-demand preview transcode, and generation.

Kept deliberately small — one process, in-memory corpus cache, vanilla-JS front
end served from ``static/``. Requires Flask (the ``clipper`` optional extra).
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path

from .. import audio, datasets
from ..clips import DEFAULT_PAD_S, ClipRequest, CueEntry, generate, media_status_for
from ..corpus import (
    Corpus,
    browsable_file,
    list_dir,
    list_roots,
    load_episodes,
    match_videos_in_directory,
    media_abspath,
)
from ..ffmpeg import BurnStyle, NormSpec, ffmpeg_available, preview
from ..search import Match, _cued, search
from ..settings import (
    CONTAINERS,
    DEFAULT_BOOKMARK_MERGE,
    DEFAULT_CONTAINER,
    MAX_BOOKMARK_MERGE,
    QUALITY_CRF,
    RESOLUTIONS,
    ClipperSettings,
    load_settings,
    save_settings,
    settings_path,
)

STATIC_DIR = Path(__file__).parent / "static"
PREVIEW_DIR = Path(tempfile.gettempdir()) / "clipper_preview"
# Everything the download endpoint is allowed to serve out of ``out_dir``.
DOWNLOAD_SUFFIXES = tuple("." + c for c in CONTAINERS) + (".srt", ".zip")


class _State:
    """Lazily loaded corpus, keyed by the (ai) flag so toggling reloads."""

    def __init__(self, config_dir: Path, out_dir: Path):
        self.config_dir = config_dir
        self.out_dir = out_dir
        self._lock = threading.Lock()
        self._corpus: Corpus | None = None
        self._ai = False
        self._subtitle_override: Path | None = None
        self._media_override: Path | None = None
        self._by_id: dict = {}
        # Custom-dataset override (single SRT / directory / bookmarks). While one
        # is installed the master corpus is set aside and search/preview/generate
        # run against the custom Corpus instead.
        self._custom: Corpus | None = None
        self._custom_kind: str | None = None
        self._custom_label: str | None = None
        self._custom_path: Path | None = None

    def corpus(self, *, ai: bool, rescan: bool = False) -> Corpus:
        # A custom dataset short-circuits the master reload — it's already resolved
        # and its media/subtitle roots are fixed for the life of the override.
        with self._lock:
            if self._custom is not None:
                return self._custom
        # The user's subtitle-root/media-root settings are read live, so saving a
        # new root in the settings menu transparently reloads the corpus on the
        # next search.
        settings = load_settings()
        sub_override = settings.subtitle_root_path()
        media_override = settings.media_root_path()
        with self._lock:
            if (self._corpus is None or ai != self._ai or rescan
                    or sub_override != self._subtitle_override
                    or media_override != self._media_override):
                self._corpus = load_episodes(
                    self.config_dir, ai=ai, rescan=rescan,
                    subtitle_root_override=sub_override,
                    media_root_override=media_override,
                )
                self._ai = ai
                self._subtitle_override = sub_override
                self._media_override = media_override
                self._by_id = {r.episode_id: r for r in self._corpus.records}
            return self._corpus

    def invalidate(self) -> None:
        """Drop the cached corpus so the next search reloads (after a settings change)."""
        with self._lock:
            self._corpus = None
            self._by_id = {}

    def install_custom(self, corpus: Corpus, kind: str, label: str,
                       path: Path | None = None) -> None:
        """Make ``corpus`` the active dataset for search/preview/generate."""
        with self._lock:
            self._custom = corpus
            self._custom_kind = kind
            self._custom_label = label
            self._custom_path = path
            self._corpus = corpus
            self._by_id = {r.episode_id: r for r in corpus.records}

    def clear_custom(self) -> None:
        """Drop the custom dataset and revert to the master corpus on next search."""
        with self._lock:
            self._custom = None
            self._custom_kind = None
            self._custom_label = None
            self._custom_path = None
            self._corpus = None
            self._by_id = {}

    def is_custom(self) -> bool:
        return self._custom is not None

    def custom_source(self) -> tuple[str | None, Path | None]:
        """The active custom dataset's (kind, path), so it can be reloaded when a
        setting that shaped it changes."""
        with self._lock:
            return self._custom_kind, self._custom_path

    def dataset_info(self) -> dict:
        with self._lock:
            active = self._custom is not None
            return {
                "active": active,
                "kind": self._custom_kind,
                "label": self._custom_label,
                "count": len(self._custom.records) if active and self._custom else 0,
            }

    def master_corpus(self) -> Corpus:
        """The master corpus, ignoring any active custom override (used to cross-
        reference directory/bookmarks filenames against known shows). Loaded
        directly so it never disturbs the cached master/custom state."""
        settings = load_settings()
        return load_episodes(
            self.config_dir,
            subtitle_root_override=settings.subtitle_root_path(),
            media_root_override=settings.media_root_path(),
        )

    def record(self, episode_id: str):
        return self._by_id.get(episode_id)


def create_app(config_dir: Path | str, out_dir: Path | str = "clips"):
    try:
        from flask import Flask, jsonify, request, send_file, send_from_directory
    except ModuleNotFoundError as e:  # pragma: no cover - import guard
        raise SystemExit(
            "Flask is required for the web UI. Install it with:\n"
            '    pip install -e ".[clipper]"'
        ) from e

    state = _State(Path(config_dir), Path(out_dir))
    # Apply the corpus's audio-track language preferences (Cantonese/Chinese by
    # default) so preview/generation pick the right stream and subtitle tag.
    audio.configure_from_dir(config_dir)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC_DIR, name)

    @app.get("/api/health")
    def health():
        return jsonify(ffmpeg=ffmpeg_available())

    @app.post("/api/search")
    def api_search():
        body = request.get_json(force=True, silent=True) or {}
        corpus = state.corpus(ai=bool(body.get("ai")), rescan=bool(body.get("rescan")))
        # Collect a large capped set; the client paginates over it. (Previously
        # the UI asked for a hard "top N" cutoff.)
        limit = min(max(int(body.get("limit", 500)), 1), 2000)
        results = search(
            corpus,
            body.get("query", ""),
            regex=bool(body.get("regex")),
            top=limit,
            exclude_shows=tuple(body.get("exclude_shows") or ()),
            only_shows=tuple(body.get("only_shows") or ()),
            require_media=bool(body.get("require_media")),
            group_versions=True,
            allow_empty=state.is_custom(),
        )
        # Linked results first (stable within each group, so corpus order holds).
        # A result counts as linked if any of its versions has a video file.
        def _linked(m):
            return m.has_video or any(v.get("has_video") for v in m.versions)
        results.sort(key=lambda m: 0 if _linked(m) else 1)
        return jsonify(
            count=len(results),
            media_root=str(corpus.media_root) if corpus.media_root else None,
            results=[m.to_dict() for m in results],
        )

    @app.get("/api/preview")
    def api_preview():
        episode_id = request.args.get("episode_id", "")
        start = float(request.args.get("start", 0.0))
        end = float(request.args.get("end", 0.0))
        pad = float(request.args.get("pad", DEFAULT_PAD_S))
        video_override = request.args.get("video_override")
        win_start_arg = request.args.get("win_start")
        win_end_arg = request.args.get("win_end")

        if video_override:
            src = Path(video_override)
        else:
            rec = state.record(episode_id)
            if rec is None:
                return jsonify(error="unknown episode"), 404
            src = media_abspath(rec, state._corpus.media_root if state._corpus else None)
        if src is None or not src.is_file():
            return jsonify(error="media not available"), 404

        win_start = float(win_start_arg) if win_start_arg is not None else max(0.0, start - pad)
        win_end = float(win_end_arg) if win_end_arg is not None else end + pad
        # Pick the audio track exactly as generation does (_select_audio_track),
        # so the preview plays the same stream the generated clip would cut.
        from ..clips import _select_audio_track
        audio_track = _select_audio_track(src)
        # Include a short hash of the source path so an overridden video doesn't
        # collide with the original episode's cached preview, and the audio track
        # so a track change (or an older cache from before track selection)
        # re-renders instead of serving stale audio.
        src_key = hex(zlib.crc32(str(src).encode("utf-8")) & 0xFFFFFFFF)[2:]
        name = (f"{episode_id or Path(str(src)).stem}_{src_key}_a{audio_track}"
                f"_{int(win_start*1000)}_{int(win_end*1000)}.mp4")
        out = PREVIEW_DIR / name
        if not out.is_file():
            preview(src, win_start, win_end, out, audio_track=audio_track)
        return send_file(out, mimetype="video/mp4", conditional=True)

    @app.get("/api/cues")
    def api_cues():
        episode_id = request.args.get("episode_id", "")
        rec = state.record(episode_id)
        if rec is None:
            return jsonify(error="unknown episode"), 404
        corpus = state._corpus or state.corpus(ai=False)
        cues = _cued(corpus.srt_abspath(rec))
        return jsonify(cues=[
            {"cue_index": c.index, "start": c.start, "end": c.end, "text": c.text}
            for c in cues
        ])

    @app.get("/api/browse")
    def api_browse():
        videos_only = request.args.get("videos_only", "").lower() in ("1", "true", "yes")
        exts_arg = request.args.get("exts", "")
        exts = tuple(e.strip().lower() for e in exts_arg.split(",") if e.strip()) or None
        raw_path = request.args.get("path", "")
        # Drive/home shortcuts travel with every listing: the ``..`` chain dead-ends
        # at a drive root, so they're the only way to cross to another drive.
        roots = list_roots()
        picked_file = None
        if raw_path:
            path = _browse_path(raw_path)
            # A typed or pasted path naming a file lands in its folder, and is
            # reported back as ``file`` so the client can take it as the pick
            # directly — but only if the picker would have listed it at all.
            if path.is_file():
                if browsable_file(path, videos_only=videos_only, exts=exts):
                    picked_file = str(path)
                path = path.parent
        else:
            corpus = state._corpus or state.corpus(ai=False)
            # Picking a subtitle/bookmarks file starts in the subtitle tree;
            # picking a video starts in the media tree.
            path = corpus.corpus_root if exts else (corpus.media_root or corpus.corpus_root)
        if not path.is_dir():
            return jsonify(error="no such folder", path=str(path), roots=roots), 404
        parent = path.parent
        return jsonify(path=str(path),
                       parent=str(parent) if parent != path else None,
                       roots=roots,
                       file=picked_file,
                       entries=list_dir(path, videos_only=videos_only, exts=exts))

    @app.get("/api/dataset")
    def api_dataset_get():
        return jsonify(**state.dataset_info())

    @app.post("/api/dataset")
    def api_dataset_set():
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("mode", "")
        raw_path = body.get("path", "")
        if not raw_path:
            return jsonify(error="missing path"), 400
        path = Path(raw_path)
        try:
            if mode == "srt":
                corpus, label = datasets.load_single_srt(path)
            elif mode == "dir":
                corpus, label = datasets.load_directory(path, state.master_corpus())
            elif mode == "bookmarks":
                corpus, label = datasets.load_bookmarks(
                    path, state.master_corpus(),
                    merge_threshold=load_settings().bookmark_merge,
                )
            else:
                return jsonify(error=f"unknown dataset mode: {mode}"), 400
        except datasets.DatasetError as e:
            return jsonify(error=str(e)), 400
        state.install_custom(corpus, mode, label, path)
        return jsonify(ok=True, kind=mode, label=label, count=len(corpus.records))

    @app.post("/api/dataset/master")
    def api_dataset_clear():
        state.clear_custom()
        return jsonify(ok=True, **state.dataset_info())

    @app.get("/api/probe")
    def api_probe():
        raw_path = request.args.get("path", "")
        if not raw_path:
            return jsonify(error="missing path"), 400
        return jsonify(status=media_status_for(Path(raw_path)))

    @app.post("/api/relink-series")
    def api_relink_series():
        body = request.get_json(force=True, silent=True) or {}
        directory = body.get("directory", "")
        items = body.get("items", [])
        if not directory:
            return jsonify(error="missing directory"), 400
        mapping = match_videos_in_directory(items, Path(directory))
        return jsonify(mapping=mapping)

    @app.post("/api/generate")
    def api_generate():
        body = request.get_json(force=True, silent=True) or {}
        pad = float(body.get("pad", DEFAULT_PAD_S))
        settings = load_settings()
        # The container is a setting, not part of the typed name — whatever
        # extension the user typed is replaced by the configured one.
        out_name = _safe_name(body.get("name") or "clip", settings.suffix())
        out_path = state.out_dir / out_name
        corpus = state._corpus or state.corpus(ai=False)

        items = []
        for d in body.get("matches", []):
            match = Match.from_dict(d)
            extra_cues = tuple(
                CueEntry(float(c["start"]), float(c["end"]), c["text"],
                         _highlights(c.get("highlights")))
                for c in d.get("extra_cues", [])
            )
            items.append(ClipRequest(
                match=match,
                win_start=d.get("win_start"),
                win_end=d.get("win_end"),
                extra_cues=extra_cues,
                video_override=d.get("video_override"),
                target_highlights=_highlights(d.get("highlights")),
            ))

        burn = body.get("burn_in")
        burn_style = BurnStyle(
            font=burn.get("font", "Arial"),
            size=int(burn.get("size", 28)),
            primary_color=burn.get("primary_color", "&H00FFFFFF"),
            outline_color=burn.get("outline_color", "&H00000000"),
            outline=int(burn.get("outline", 2)),
        ) if burn else None

        width, height = settings.dimensions()
        spec = NormSpec(width=width, height=height, crf=settings.crf())
        separate = bool(body.get("separate"))
        # Bundling only means something when there are several files to bundle.
        as_zip = separate and bool(body.get("zip"))

        if as_zip:
            # Per-clip files go to a temp dir; only the bundled zip lands in clips/.
            with tempfile.TemporaryDirectory(prefix="clipper_zip_") as tmp:
                report = generate(
                    items, corpus.media_root, Path(tmp) / out_name,
                    pad_s=pad, spec=spec, records_by_id=state._by_id,
                    burn_in=burn_style, separate=True,
                )
                downloads = []
                if report.outputs:
                    zip_name = _safe_name(body.get("name") or "clips", ".zip")
                    state.out_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(state.out_dir / zip_name, "w",
                                         zipfile.ZIP_DEFLATED) as zf:
                        for out, srt in zip(report.outputs, report.srts):
                            zf.write(out, out.name)
                            if srt and srt.is_file():
                                zf.write(srt, srt.name)
                    downloads = [zip_name]
        else:
            # Both the single-file and the unbundled per-clip case write straight
            # into out_dir, so every file is individually downloadable.
            report = generate(
                items, corpus.media_root, out_path,
                pad_s=pad, spec=spec, records_by_id=state._by_id,
                burn_in=burn_style, separate=separate,
            )
            downloads = []
            for out, srt in zip(report.outputs, report.srts):
                downloads.append(out.name)
                if srt and srt.is_file():
                    downloads.append(srt.name)

        return jsonify(
            generated=len(report.included),
            output=str(report.output) if report.output else None,
            srt=str(report.srt) if report.srt else None,
            download=downloads[0] if downloads else None,
            downloads=downloads,
            outputs=[o.name for o in report.outputs],
            separate=separate,
            zipped=as_zip,
            skipped=[{"label": s.label, "reason": s.reason} for s in report.skipped],
        )

    @app.get("/api/download/<path:name>")
    def api_download(name):
        # Preserve the actual extension; fall back to the configured container.
        suffix = Path(name).suffix.lower()
        safe = _safe_name(
            name, suffix if suffix in DOWNLOAD_SUFFIXES else load_settings().suffix()
        )
        target = (state.out_dir / safe).resolve()
        if not target.is_file() or target.parent != state.out_dir.resolve():
            return jsonify(error="not found"), 404
        return send_file(target, as_attachment=True)

    @app.get("/api/settings")
    def api_get_settings():
        s = load_settings()
        eff_media = state._corpus.media_root if state._corpus else s.media_root_path()
        eff_sub = state._corpus.corpus_root if state._corpus else s.subtitle_root_path()
        return jsonify(
            subtitle_root=s.subtitle_root or "",
            media_root=s.media_root or "",
            resolution=s.resolution,
            quality=s.quality,
            container=s.container,
            bookmark_merge=s.bookmark_merge,
            resolutions=list(RESOLUTIONS),
            qualities=list(QUALITY_CRF),
            containers=list(CONTAINERS),
            max_bookmark_merge=MAX_BOOKMARK_MERGE,
            effective_subtitle_root=str(eff_sub) if eff_sub else None,
            effective_media_root=str(eff_media) if eff_media else None,
            path=str(settings_path()),
        )

    @app.post("/api/settings")
    def api_set_settings():
        body = request.get_json(force=True, silent=True) or {}
        s = ClipperSettings(
            subtitle_root=(body.get("subtitle_root") or "").strip() or None,
            media_root=(body.get("media_root") or "").strip() or None,
            resolution=str(body.get("resolution", "720p")),
            quality=str(body.get("quality", "medium")),
            container=str(body.get("container", DEFAULT_CONTAINER)),
            bookmark_merge=_int_or(body.get("bookmark_merge"),
                                   DEFAULT_BOOKMARK_MERGE),
        )
        # load_settings() clamps invalid enums to defaults; round-trip so the
        # saved file (and the response) always reflect validated values.
        path = save_settings(s)
        state.invalidate()
        # The bookmark grouping is baked in when the file is read, so an active
        # bookmarks dataset has to be re-read for a new threshold to show up.
        reloaded = _reload_bookmarks_dataset()
        return jsonify(ok=True, path=str(path), dataset=reloaded,
                       **_settings_payload())

    @app.post("/api/refresh")
    def api_refresh():
        # Rebuild the searchable corpus from scratch (in memory only — never
        # touches the tracked config/data). Uses the current AI toggle.
        corpus = state.corpus(ai=state._ai, rescan=True)
        return jsonify(
            ok=True,
            count=len(corpus.records),
            media_root=str(corpus.media_root) if corpus.media_root else None,
        )

    def _settings_payload() -> dict:
        s = load_settings()
        return {"subtitle_root": s.subtitle_root or "", "media_root": s.media_root or "",
                "resolution": s.resolution, "quality": s.quality,
                "container": s.container, "bookmark_merge": s.bookmark_merge}

    def _reload_bookmarks_dataset() -> dict | None:
        """Re-read the active .SE.bookmarks dataset (if any) with the settings as
        they stand now. Returns the new dataset info, or None if nothing to do."""
        kind, path = state.custom_source()
        if kind != "bookmarks" or path is None:
            return None
        try:
            corpus, label = datasets.load_bookmarks(
                path, state.master_corpus(),
                merge_threshold=load_settings().bookmark_merge,
            )
        except datasets.DatasetError:
            return None      # file moved/changed since it was loaded; keep the old one
        state.install_custom(corpus, kind, label, path)
        return state.dataset_info()

    return app


def _highlights(raw) -> tuple[tuple[int, int], ...]:
    """Coerce the JSON ``[[start, end], ...]`` highlight ranges to a tuple.

    Bounds checking and merging happen in :func:`clips.normalize_highlights`,
    which knows the text length; this only shapes the input.
    """
    out: list[tuple[int, int]] = []
    for r in raw or ():
        try:
            out.append((int(r[0]), int(r[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return tuple(out)


_RE_BARE_DRIVE = re.compile(r"[A-Za-z]:")


def _browse_path(raw: str) -> Path:
    """Turn a hand-typed browse path into something listable.

    Typed paths arrive in whatever shape the user's shell or file manager hands
    out, so: surrounding quotes (Windows' "Copy as path" adds them) are dropped,
    ``~`` and ``%VAR%``/``$VAR`` are expanded, and a bare drive letter is
    completed to that drive's root — ``E:`` alone means "the current directory
    on E:", which is not what anyone typing it into a folder box intends.
    """
    text = os.path.expandvars(raw.strip().strip('"'))
    if _RE_BARE_DRIVE.fullmatch(text):
        text += os.sep
    return Path(text).expanduser()


def _int_or(value, default: int) -> int:
    """Coerce a JSON field to int; settings.load_settings clamps the range."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_name(name: str, suffix: str = ".mp4") -> str:
    """Reject path traversal; keep just the basename with the given suffix.

    Any known output extension is stripped first so the caller's ``suffix``
    always wins (e.g. ``clip.mkv`` -> ``clip.zip``). Without the strip, a name
    the user typed with an extension would end up doubled (``clip.mp4.mkv``)."""
    base = Path(name).name
    low = base.lower()
    for ext in DOWNLOAD_SUFFIXES:
        if low.endswith(ext):
            base = base[: -len(ext)]
            break
    return base + suffix


def serve(config_dir: Path | str, *, host: str = "127.0.0.1", port: int = 8765,
          out_dir: Path | str = "clips", open_browser: bool = True) -> None:
    app = create_app(config_dir, out_dir=out_dir)
    # 0.0.0.0 binds all interfaces but isn't a valid address to browse to.
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{browse_host}:{port}"
    print(f"Subtitle Clip Studio → {url}  (Ctrl+C to stop)")
    if not ffmpeg_available():
        print("WARNING: ffmpeg/ffprobe not found on PATH — preview and generation will fail.")
    if open_browser:
        _open_when_ready(url)
    app.run(host=host, port=port, threaded=True)


def _open_when_ready(url: str) -> None:
    """Open the UI in a browser once the server is accepting connections."""
    import webbrowser

    def worker() -> None:
        import socket
        import time
        from urllib.parse import urlparse

        parsed = urlparse(url)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.15)
        webbrowser.open(url)

    threading.Thread(target=worker, daemon=True).start()
