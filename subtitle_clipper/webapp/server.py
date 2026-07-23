"""Flask app: search JSON API, on-demand preview transcode, and generation.

Kept deliberately small — one process, in-memory corpus cache, vanilla-JS front
end served from ``static/``. Requires Flask (the ``clipper`` optional extra).
"""

from __future__ import annotations

import tempfile
import threading
import zlib
from pathlib import Path

from .. import audio
from ..clips import DEFAULT_PAD_S, ClipRequest, CueEntry, generate, media_status_for
from ..corpus import Corpus, list_dir, load_episodes, match_videos_in_directory, media_abspath
from ..ffmpeg import BurnStyle, NormSpec, ffmpeg_available, preview
from ..search import Match, _cued, search
from ..settings import (
    QUALITY_CRF,
    RESOLUTIONS,
    ClipperSettings,
    load_settings,
    save_settings,
    settings_path,
)

STATIC_DIR = Path(__file__).parent / "static"
PREVIEW_DIR = Path(tempfile.gettempdir()) / "clipper_preview"


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

    def corpus(self, *, ai: bool, rescan: bool = False) -> Corpus:
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
        raw_path = request.args.get("path", "")
        if raw_path:
            path = Path(raw_path)
        else:
            corpus = state._corpus or state.corpus(ai=False)
            path = corpus.media_root or corpus.corpus_root
        if not path.is_dir():
            return jsonify(error="not a directory", path=str(path)), 404
        return jsonify(path=str(path), entries=list_dir(path, videos_only=videos_only))

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
        out_name = _safe_name(body.get("name") or "clip.mkv")
        out_path = state.out_dir / out_name
        corpus = state._corpus or state.corpus(ai=False)

        items = []
        for d in body.get("matches", []):
            match = Match.from_dict(d)
            extra_cues = tuple(
                CueEntry(float(c["start"]), float(c["end"]), c["text"])
                for c in d.get("extra_cues", [])
            )
            items.append(ClipRequest(
                match=match,
                win_start=d.get("win_start"),
                win_end=d.get("win_end"),
                extra_cues=extra_cues,
                video_override=d.get("video_override"),
            ))

        burn = body.get("burn_in")
        burn_style = BurnStyle(
            font=burn.get("font", "Arial"),
            size=int(burn.get("size", 28)),
            primary_color=burn.get("primary_color", "&H00FFFFFF"),
            outline_color=burn.get("outline_color", "&H00000000"),
            outline=int(burn.get("outline", 2)),
        ) if burn else None

        settings = load_settings()
        width, height = settings.dimensions()
        spec = NormSpec(width=width, height=height, crf=settings.crf())
        report = generate(
            items, corpus.media_root, out_path,
            pad_s=pad, spec=spec, records_by_id=state._by_id, burn_in=burn_style,
        )
        return jsonify(
            generated=len(report.included),
            output=str(report.output) if report.output else None,
            srt=str(report.srt) if report.srt else None,
            download=out_name if report.output else None,
            skipped=[{"label": s.label, "reason": s.reason} for s in report.skipped],
        )

    @app.get("/api/download/<path:name>")
    def api_download(name):
        safe = _safe_name(name)
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
            resolutions=list(RESOLUTIONS),
            qualities=list(QUALITY_CRF),
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
        )
        # load_settings() clamps invalid enums to defaults; round-trip so the
        # saved file (and the response) always reflect validated values.
        path = save_settings(s)
        state.invalidate()
        return jsonify(ok=True, path=str(path), **_settings_payload())

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
                "resolution": s.resolution, "quality": s.quality}

    return app


def _safe_name(name: str) -> str:
    """Reject path traversal; keep just the basename with a safe suffix."""
    base = Path(name).name
    if not base.lower().endswith(".mkv"):
        base += ".mkv"
    return base


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
