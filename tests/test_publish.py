"""publish.py uploads an already signed version again instead of asking release.py to rebuild it."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from tilingvol import __version__

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"


@pytest.fixture
def publish(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("publish", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RELEASE_DIR", tmp_path)
    return module


def write_release(folder: Path, version: str, content: bytes) -> Path:
    app = folder / f"fe70-{version}-windows.exe"
    app.write_bytes(content)
    entry = {"filename": app.name, "sha256": hashlib.sha256(content).hexdigest()}
    manifest = {"version": version, "platforms": {"windows": entry}}
    (folder / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app


def test_nothing_signed_yet(publish):
    assert not publish.already_signed()


def test_an_older_version_is_built_anew(publish, tmp_path):
    write_release(tmp_path, "0.0.1", b"app")
    assert not publish.already_signed()


def test_this_version_is_uploaded_again(publish, tmp_path):
    write_release(tmp_path, __version__, b"app")
    assert publish.already_signed()


def test_a_changed_file_is_refused(publish, tmp_path):
    write_release(tmp_path, __version__, b"app").write_bytes(b"other")
    with pytest.raises(SystemExit, match="differs"):
        publish.already_signed()
