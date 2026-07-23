# Subtitle Clip Studio

A standalone tool for **searching a subtitle corpus** and **cutting the matching
lines out of the linked source videos** into a single stitched clip with an
aligned, embedded subtitle track.

Point it at a master directory of SRTs laid out by show / season / episode, and
it scans that tree into a searchable index. Search for a phrase, pick the lines
you want, preview them, and generate one MKV that stitches those moments together
with a combined subtitle track. The corpus is only ever **read** — generated
clips go to a separate output directory (default `./clips`).

It ships tuned for a Cantonese subtitle library (audio-track selection prefers
Cantonese, then any Chinese track), but nothing is hard-coded: the corpus
location, exclusion rules, and audio-language preferences all live in editable
config files, so any subtitle library with a similar folder structure works.

## Install

```bash
pip install -e .
```

`ffmpeg` and `ffprobe` must be on your PATH. Search works without them;
**preview** and **generate** require them.

## Point it at your subtitles

Edit [`config/corpus.toml`](config/corpus.toml) and set `root` to your subtitle
directory (or leave the config alone and set the **Subtitle root** in the app's
settings menu, or pass `clipper search --subtitle-root DIR`).

Expected layout (adapt `include_roots` / `exclude_globs` to your own):

```
<root>/Series/Original/Show Name -- 中文名 (2003)/Season 1/Show.S01E01.BD.yue.srt
<root>/Movies/Dubbed/A Movie (2001)/A Movie.2001.BD.yue.srt
```

Filenames are parsed for `SxxEyy` / episode numbers, release source
(`BD`/`DVD`/`NF`/…) and language tokens. To link clips to video, set `media_root`
in [`config/media_map.toml`](config/media_map.toml) (or the **Media root**
setting) to a directory that mirrors the subtitle tree.

## Web UI (recommended)

```bash
clipper serve                 # starts the server and opens http://127.0.0.1:8765
clipper serve --no-open       # start without launching a browser
clipper serve --port 9000     # pick a different port
```

Search, tick the results you want (rows without a linked video are disabled and
badged), preview each inline, then **Generate from selected**. The output MKV
and a matching `.srt` sidecar are written to `./clips` with a download link.
Results with a linked video are listed **first**. Matches are **paginated**
(choose items-per-page); "Select all on page" toggles the current page and
"Select all results" toggles every selectable row across all pages. The
searched text is highlighted in each result line.

**Settings** (⚙): set the **Subtitle root** (master subtitle directory) and
**Media root** without editing config files, choose the output resolution /
quality, and **Refresh subtitle data** to re-index after changing files. Roots
saved here are stored per-user (see *Settings storage* below) and take effect on
the next search.

**Multiple versions**: when a show exists in several releases (e.g. BD, DVD and
Netflix), the same line is collapsed into **one result** rather than one per
release. A version dropdown lets you switch which release is used — the preferred
order is BD > DVD > streaming, so the best master is selected by default.
Switching a version re-reads that release's timing and media (and clears any
manual timing/text/file overrides on that row).

**Custom linking** (session-only — nothing is persisted, so it resets on page
reload): click a result's video path to browse the server's filesystem and pick
a replacement file for that one result (with an option to apply it to every other
result from the same episode); click a show's name to pick a directory for the
whole series, which re-pairs each episode by season/episode number — episodes
with no confident match come back unlinked rather than erroring. Re-pairing is
**version-aware**: if the picked folder holds several releases side by side
(`Season 1 - BD (sync)` / `… - DVD (sync)` / `… - Netflix`), each episode is
matched to the file whose name/folder matches its selected version, while a plain
`S1` / `Season 1` folder still matches by default. Links you make are
**remembered for the session** until you refresh the page.

**Item timing**: "Edit timing ▾" on a result shows its cut window (default
`[start − pad, end + pad]`, entered/shown as `HH:MM:SS.mmm`) with editable
start/end fields. Edits are clamped so the window can never crop into the line's
own SRT bounds, and any line from the same episode that ends up wholly inside
the window is auto-added as an extra subtitle for that clip. If the default
padded start lands in the *middle* of a neighboring line, the window is
automatically pulled back to that line's start so it's shown whole. Editing the
timing also re-includes any lines you had removed.

**Line editing**: any line's text can be edited inline in the timing panel;
edited lines are marked and have a ↺ button to restore the original. Edits flow
into both the subtitle track and the `.srt` sidecar.

**Burn-in**: the gen-bar's "Burn in subtitles" checkbox switches from muxing a
soft `.srt` track to hardcoding the subtitles into the frame (font/size/color/
outline configurable). When burn-in is on, inline previews show the subtitles too
(rendered as a styled WebVTT track over the `<video>`).

## CLI

```bash
# Search; results are printed and cached to .clipper_last_search.json
clipper search "邊個" --top 10
clipper search "rain" --regex --require-media --only-show "Show Name"
clipper search "hello" --subtitle-root "D:/OtherSubs"   # ad-hoc corpus

# Cut the last search (or a --select subset) into one stitched MKV
clipper generate --from last --select 1,3,5-8 --pad 0.5 --out clips/rain.mkv
```

Key search flags: `--regex`, `--top N`, `--only-show NAME` / `--exclude-show
NAME` (repeatable), `--require-media` (drop matches with no video),
`--subtitle-root DIR` / `--media-root DIR` (override the configured roots),
`--ai` (include AI-generated subtitles hidden by the exclude globs), `--rescan`.

## How generation works

For each selected item with a usable video file (corpus-linked or session-only
overridden):

1. cut its window — `[start − pad, end + pad]` by default, or the item's edited
   `win_start`/`win_end` — and **re-encode** to a normalized temp clip (common
   codec / resolution / fps / sample rate) so heterogeneous sources can be joined;
2. concatenate the clips (concat demuxer, stream copy);
3. build a combined SRT whose timestamps track the stitched timeline;
4. either mux that SRT back in as a soft subtitle track, or burn it directly into
   the frame if burn-in is enabled.

The **audio track** picked for each cut is the preferred stream configured in the
`[audio]` table of `config/corpus.toml` — by default the Cantonese stream if one
is tagged (`language=yue` / a "Cantonese" title); otherwise the first Chinese
track of any kind; failing that, ffmpeg's default. Edit that table to localize
for another language. The muxed subtitle track's language tag is configurable
there too (`subtitle_language_tag`, default `yue`).

Items whose linked media is **missing** or **audio-only** are skipped and
reported, never silently dropped. Output is always MKV.

## Settings storage

Per-user settings (subtitle root, media root, output resolution/quality) are
written to `%APPDATA%/subtitle-clip-studio/clipper.toml` on Windows, else
`$XDG_CONFIG_HOME/subtitle-clip-studio/clipper.toml` (falling back to
`~/.config/...`). They override the tracked `config/*.toml` without modifying it.

## Module layout

| File | Responsibility |
|------|----------------|
| `config.py` | Typed loaders for `corpus.toml` / `media_map.toml`, and the `[audio]` language-preference config. |
| `manifest.py` | Corpus scanner: walk the subtitle tree, parse filenames, pair media into `EpisodeRecord`s. |
| `srt.py` | Tolerant SRT parser (`parse_srt` → `Cue`s). |
| `audio.py` | ffprobe audio-track probing + language-aware track selection (configurable). |
| `corpus.py` | Load episode records (scan or manifest cache), resolve media paths, directory browsing and series re-pairing. |
| `search.py` | `Match` + `search()` over cues (substring/regex, filters, top-N, version-collapsing). |
| `ffmpeg.py` | Thin, testable ffmpeg/ffprobe command builders and runners (cut / concat / mux / burn-in / preview). |
| `clips.py`  | Generation: per-item overrides, combined-SRT builder, orchestration, skip report. |
| `webapp/`   | Flask JSON API + on-demand preview transcode + the vanilla-JS front end. |
| `__main__.py` | `clipper` CLI: `serve` / `search` / `generate`. |

Tests live in `tests/` and need no ffmpeg or media (subprocess calls are asserted
on / guarded):

```bash
pytest
```
