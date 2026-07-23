"""Tolerant SRT parser.

Handles the corpus's real-world quirks: UTF-8 BOM, CRLF, `.` as the millisecond
separator (Netflix exports), HTML entities (&lrm; etc.), bidi control chars,
missing/garbled index lines, and blank cues. Pure-Python `str` handling is
astral-plane safe (𠻺 and friends are single code points).
"""

import html
import re
from dataclasses import dataclass
from pathlib import Path

_TIMESTAMP = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
_RE_TIMING = re.compile(rf"^\s*{_TIMESTAMP}\s*-->\s*{_TIMESTAMP}")
# Zero-width and bidi controls that sneak in via &lrm; entities or copy-paste.
_RE_INVISIBLE = re.compile("[\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069\\ufeff]")

_ENCODINGS = ("utf-8-sig", "utf-16", "big5")


@dataclass
class Cue:
    index: int          # position in file (0-based), not the SRT counter
    start: float        # seconds
    end: float
    raw: str            # text exactly as read (lines joined with \n)
    text: str           # entity-unescaped, control-stripped
    number: int | None = None  # printed SRT sequence counter, when present


class SrtParseError(ValueError):
    pass


def _decode(data: bytes, path: Path) -> str:
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise SrtParseError(f"{path}: could not decode with any of {_ENCODINGS}")


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(path: Path) -> list[Cue]:
    text = _decode(path.read_bytes(), path).replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    block: list[str] = []

    def flush(block: list[str]) -> None:
        timing_i = next(
            (i for i, line in enumerate(block) if _RE_TIMING.match(line)), None
        )
        if timing_i is None:
            return  # header junk or stray counter with no timing line
        m = _RE_TIMING.match(block[timing_i])
        start = _to_seconds(*m.groups()[:4])
        end = _to_seconds(*m.groups()[4:])
        raw = "\n".join(block[timing_i + 1:]).strip("\n")
        cleaned = _RE_INVISIBLE.sub("", html.unescape(raw)).strip()
        if not cleaned:
            return
        # The line directly above the timing line is the SRT sequence counter
        # when it's a bare integer (SubtitleEdit bookmarks reference this number).
        number = None
        if timing_i > 0:
            prev = block[timing_i - 1].strip()
            if prev.isdigit():
                number = int(prev)
        cues.append(Cue(index=len(cues), start=start, end=end, raw=raw,
                        text=cleaned, number=number))

    for line in text.split("\n"):
        if line.strip() == "":
            if block:
                flush(block)
                block = []
        else:
            block.append(line)
    if block:
        flush(block)

    if not cues:
        raise SrtParseError(f"{path}: no parseable cues")
    return cues
