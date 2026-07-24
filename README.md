# Subtitle Clip Studio

Search a pile of subtitle files for a word or phrase, tick the lines you like, and
get back a single video clip that stitches those exact moments together — with the
subtitles baked in or muxed alongside.

<!-- demo GIF goes here -->

It runs as a small web app on your own machine. Your subtitle and video folders are
only ever **read**; finished clips are written to a separate `clips/` folder.

## Why it exists

This was built to make clips from the [CantoCaptions](https://cantocaptions.com)
Cantonese subtitle library — search for a word, and instantly see (and export) every
scene where a real person actually says it. It still ships tuned for that use case:
generated clips prefer the Cantonese audio track when a video has several. But
nothing is hard-coded, so any subtitle library organized by show / season / episode
works just as well.

## A few nice things it does

- Preview every matching line inline before you commit to a clip.
- Nudge each cut's start and end so lines aren't chopped off mid-sentence.
- Edit subtitle text inline — your edits go into the exported clip.
- Burn subtitles into the picture, or keep them as a switchable track.
- Export one stitched video, or one file per line as a zip.
- Search a single SRT, a loose folder of SRTs, or your SubtitleEdit bookmarks
  instead of the whole library.

## Quickstart

**1. Install [Python](https://www.python.org/downloads/) 3.11 or newer**, and
install [ffmpeg](https://ffmpeg.org/download.html) so that `ffmpeg` and `ffprobe`
are on your PATH. (Searching works without ffmpeg; previewing and making clips
does not.)

**2. Install the app.** In a terminal, from this folder:

```bash
pip install -e .
```

**3. Start it.**

```bash
clipper serve
```

Your browser opens to `http://127.0.0.1:8765`.

**4. Tell it where your files are.** This part is required — open the **⚙ settings**
menu in the app and set:

- **Subtitle root** — the folder holding your subtitle (`.srt`) files.
- **Media root** — the folder holding the matching videos. Its folders should
  mirror the subtitle folders; episodes are paired up by their `S01E02`-style
  numbering. Leave it empty if you only want to search text.

**5. Search, tick the lines you want, and hit "Generate from selected."** The clip
and a matching `.srt` land in `clips/` with a download link.

> Settings you save in the app are stored per-user and override the files in
> [`config/`](config/), which are there if you'd rather edit roots, folder
> exclusions, or audio-language preferences by hand.

## Command line

Everything the web UI does is also available as a CLI, if you prefer:

```bash
clipper search "邊個" --top 10
clipper generate --from last --select 1,3,5-8 --out clips/rain.mkv
```

Run `clipper search --help` or `clipper generate --help` for the full flag list.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests need no ffmpeg and no media files.

## License

MIT — see [LICENSE](LICENSE).
