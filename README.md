# Subtitle Clip Studio

Search a collection of subtitle files (.SRT) for a word or phrase, select your preferred lines, and
get back a single video clip that stitches them together. You'll need to bring your own video files,
of course.

https://github.com/user-attachments/assets/0a4e2606-e91b-44fd-bcec-83e1f44cc103

> **Note**: This tool was built to clip subtitles from the [CantoCaptions](https://cantocaptions.com)
> Cantonese subtitle library, so it may require some tweaking to use for other subtitles sets.
> You can manage custom configurations by editing the TOML files in the `config` folder by hand.
> Default CantoCaptions configurations include: 
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
- Bookmarks on neighbouring lines are listed as one entry, padded as a whole, with each line kept as
  its own subtitle. Settings ⚙ sets how far apart bookmarks may be and still share an entry: at the
  default of 1 only back-to-back bookmarks merge, while e.g. 3 turns bookmarks on lines 10, 11, 14,
  18 and 20 into two entries (10–14 and 18–20), taking in the unbookmarked lines between them.

## Quickstart

#### 1. Install Prerequisites

* [Python](https://www.python.org/downloads/) 3.11 or newer
* [ffmpeg](https://ffmpeg.org/download.html). Ensure `ffmpeg` and `ffprobe`
are on your PATH.

#### 2. Clone

Run the following commands from a terminal window.

```bash
git clone https://github.com/rookes/subtitle-clip-studio.git
cd subtitle-clip-studio
```

#### 3. Install & Run

```bash
pip install -e .
clipper serve
```

This will run the app server on your local machine and open a browser to `http://127.0.0.1:8765`. 

#### 4. Configure

Open the **⚙ settings** menu in the app and set:

- **Subtitle root** — the folder holding your subtitle (`.srt`) files. For CantoCaptions, set this to
  the `CantoCaptions/Subtitle` directory.
- **Media root** — the folder holding the matching videos. If the media root's folders
  mirror the subtitle folders, then videos will be automatically linked for clipping. Otherwise,
  you'll need to manually select your video location each time. A show folder named differently
  on the media side (`Bluey` for `Bluey -- 妙妙犬保怡 (2018)`) is still found, by matching the
  title and year. Season folders are binding: a file under `S2/` is only ever offered for
  season 2, so a season you have no video for stays unlinked rather than borrowing another
  season's episode of the same number.

  Make sure to hit "Save" and click to refresh the database after you update the root directory.

#### 5. Search, Select, and Generate

Now you can search for text that matches any SRT in your collection. Once you run a search, all lines
will display by default, even if there isn't any matching video. You may need manually select the media 
location, either for each show or for individual files.

Once you've selected and adjusted your choice of subtitles clips, you can hit the "Generate from selected" 
button to generate a compiled video (or a collection of videos if you select that option).

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
