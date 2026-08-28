"""The web UI's /api/browse endpoint (subtitle_clipper.webapp.server).

Covers the path-entry conveniences only; every request here passes an explicit
``path`` so the corpus is never loaded.
"""

import os
from pathlib import Path

import pytest

from subtitle_clipper.webapp.server import _browse_path, create_app


@pytest.fixture
def client(tmp_path):
    return create_app(tmp_path / "config", out_dir=tmp_path / "clips").test_client()


def test_browse_path_strips_quotes_and_expands_vars(monkeypatch, tmp_path):
    # Windows' "Copy as path" wraps the path in quotes; shells hand out %VARS%.
    monkeypatch.setenv("CLIPPER_TEST_ROOT", str(tmp_path))
    var = "%CLIPPER_TEST_ROOT%" if os.name == "nt" else "$CLIPPER_TEST_ROOT"
    assert _browse_path(f'  "{var}"  ') == tmp_path


def test_browse_path_expands_tilde():
    assert _browse_path("~") == Path.home()


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows-only")
def test_browse_path_completes_a_bare_drive_letter():
    # "E:" alone means "the current directory on E:", not its root.
    assert _browse_path("E:") == Path("E:\\")


def test_browse_lists_a_typed_directory(client, tmp_path):
    (tmp_path / "ep.mkv").write_bytes(b"")
    data = client.get("/api/browse", query_string={"path": str(tmp_path),
                                                   "videos_only": "1"}).get_json()
    assert data["path"] == str(tmp_path)
    assert data["parent"] == str(tmp_path.parent)
    assert [e["name"] for e in data["entries"] if not e["is_dir"]] == ["ep.mkv"]


def test_browse_offers_roots_so_another_drive_is_reachable(client, tmp_path):
    # The ".." chain dead-ends at a drive root, so these shortcuts are the only
    # way out of the drive the picker happens to start on.
    data = client.get("/api/browse", query_string={"path": str(tmp_path)}).get_json()
    assert data["roots"]
    assert all(Path(r["path"]).is_dir() for r in data["roots"])


def test_browse_takes_a_typed_file_path_as_the_pick(client, tmp_path):
    (tmp_path / "ep.mkv").write_bytes(b"")
    data = client.get("/api/browse", query_string={"path": str(tmp_path / "ep.mkv"),
                                                   "videos_only": "1"}).get_json()
    assert data["file"] == str(tmp_path / "ep.mkv")
    assert data["path"] == str(tmp_path)        # and lands in its folder


def test_browse_ignores_a_typed_file_the_filter_excludes(client, tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"")
    data = client.get("/api/browse", query_string={"path": str(tmp_path / "notes.txt"),
                                                   "videos_only": "1"}).get_json()
    assert data["file"] is None
    assert data["path"] == str(tmp_path)


def test_browse_missing_folder_still_returns_roots(client, tmp_path):
    r = client.get("/api/browse", query_string={"path": str(tmp_path / "nope")})
    assert r.status_code == 404
    assert r.get_json()["roots"]                # so the modal keeps its shortcuts
