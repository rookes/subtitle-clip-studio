# Subtitle Clip Studio

Search a collection of subtitle files (.SRT) for a word or phrase, select your preferred lines, and
get back a single video clip that stitches them together. You'll need to bring your own video files,
of course.

https://github.com/user-attachments/assets/0a4e2606-e91b-44fd-bcec-83e1f44cc103

> Note: This tool was built to clip subtitles from the [CantoCaptions](https://cantocaptions.com)
> Cantonese subtitle library, so it may require some configuration tweaking to use for other 
> subtitles. You can manage these configurations by updating the TOML files in the `config` folder.
> Default CantoCaptions configuration includes: 
> 
> * Cantonese audio tracks are selected automatically when a video has multiple.
> * Show names, season and episode numbers are identified automatically when the
>  folder structure matches the CantoCaptions dataset.
> * Non-Cantonese subtitles and other subtitles marked as AI, WIP, etc. are excluded (see exclusions
>  in `config/corpus.toml` under `exclude_globs`.

## Features

- Make custom edits on subtitle text and start/end time for each clip 
- Clip video preview in-browser
- Burn subtitles into the video or embed them as a separate track.
- Export one stitched video, or one file per line as a zip.
- Load a directory of SRT files, a single SRT file, or a SubtitleEdit bookmarks file (.SE.bookmarks) to
  be clipped.

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

This command runs the app server on your machine and opens a browser to `http://127.0.0.1:8765`. 
See more options with `clipper server --help`.

**4. Tell it where your files are.** This part is required — open the **⚙ settings**
menu in the app and set:

- **Subtitle root** — the folder holding your subtitle (`.srt`) files. For CantoCaptions, set this to
  the `CantoCaptions/Subtitle` directory.
- **Media root** — the folder holding the matching videos. If the media root's folders
  mirror the subtitle folders, then videos will be automatically linked for clipping. Otherwise,
  you'll need to manually select your video location each time.

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

Tests don't need ffmpeg or media files.

## License

MIT — see [LICENSE](LICENSE).
