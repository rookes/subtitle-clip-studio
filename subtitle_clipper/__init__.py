"""Subtitle Clip Studio.

A standalone, user-facing tool for searching a subtitle corpus (a directory of
SRTs laid out by show/season/episode) and cutting the matching lines out of the
linked source videos into a single stitched clip with an aligned, embedded
subtitle track.

Point it at a master subtitle directory (config/corpus.toml, or the Subtitle
root setting) and it scans that tree into searchable episode records. Generated
clips go to a separate output directory; the corpus is only ever read.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
