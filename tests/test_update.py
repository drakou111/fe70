"""Self-update: only signed, newer, checksum-correct releases are installed."""

import hashlib
import json
import secrets
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tilingvol.client.update import (
    UpdateError,
    Updater,
    old_path,
    parse_release,
    release_message,
    remove_previous_version,
    verify_release,
)

NEW_VERSION = "9.9.9"


def public_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def publish(
    folder: Path,
    key: Ed25519PrivateKey,
    payload: bytes,
    version: str = NEW_VERSION,
    platform: str = "windows",
) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"GunTilingSearch-{version}-{platform}.exe"
    (folder / name).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    signature = key.sign(
        release_message(version, platform, name, digest, len(payload))
    ).hex()
    manifest = {
        "version": version,
        "platforms": {
            platform: {
                "filename": name,
                "sha256": digest,
                "size": len(payload),
                "signature": signature,
            }
        },
    }
    (folder / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


@pytest.fixture
def release_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def installed(tmp_path) -> Path:
    app = tmp_path / "app" / "GunTilingSearch.exe"
    app.parent.mkdir()
    app.write_bytes(b"old version")
    return app


def test_signature_must_match_the_project_key(tmp_path, release_key):
    manifest = publish(tmp_path, release_key, b"new app")
    release = parse_release(manifest, "windows")
    verify_release(release, public_bytes(release_key))
    with pytest.raises(UpdateError):
        verify_release(release, public_bytes(Ed25519PrivateKey.generate()))
    manifest["platforms"]["windows"]["sha256"] = "0" * 64
    with pytest.raises(UpdateError):
        verify_release(parse_release(manifest, "windows"), public_bytes(release_key))


def test_malformed_manifests_are_rejected(tmp_path, release_key):
    manifest = publish(tmp_path, release_key, b"new app")
    manifest["platforms"]["windows"]["filename"] = "../../evil.exe"
    with pytest.raises(UpdateError):
        parse_release(manifest, "windows")
    assert parse_release({"version": "9.9.9", "platforms": {}}, "windows") is None


def test_download_install_and_cleanup(
    sib_config, make_live_server, tmp_path, release_key, installed
):
    payload = secrets.token_bytes(300_000)
    publish(tmp_path / "downloads", release_key, payload)
    server = make_live_server(sib_config, download_dir=tmp_path / "downloads")
    updater = Updater(
        server.url,
        public_bytes(release_key),
        installed,
        current_version="0.1.0",
        platform="windows",
    )
    release = updater.check()
    assert release is not None and release.version == NEW_VERSION
    progress = []
    downloaded = updater.download(release, progress.append)
    assert progress[-1] == 1.0
    updater.install(downloaded)
    assert installed.read_bytes() == payload
    assert old_path(installed).read_bytes() == b"old version"
    remove_previous_version(installed)
    assert not old_path(installed).exists()


def test_tampered_file_is_discarded(
    sib_config, make_live_server, tmp_path, release_key, installed
):
    folder = tmp_path / "downloads"
    manifest = publish(folder, release_key, b"genuine app" * 1000)
    (folder / manifest["platforms"]["windows"]["filename"]).write_bytes(
        b"malware!!!!" * 1000
    )  # same size
    server = make_live_server(sib_config, download_dir=folder)
    updater = Updater(
        server.url,
        public_bytes(release_key),
        installed,
        current_version="0.1.0",
        platform="windows",
    )
    with pytest.raises(UpdateError):
        updater.download(updater.check())
    assert installed.read_bytes() == b"old version"
    assert not list(installed.parent.glob("*.download"))


def test_same_or_older_versions_are_ignored(
    sib_config, make_live_server, tmp_path, release_key, installed
):
    publish(tmp_path / "downloads", release_key, b"app", version="0.1.0")
    server = make_live_server(sib_config, download_dir=tmp_path / "downloads")
    updater = Updater(
        server.url,
        public_bytes(release_key),
        installed,
        current_version="0.1.0",
        platform="windows",
    )
    assert updater.check() is None


def test_no_release_and_page_links(
    sib_config, make_live_server, tmp_path, release_key, installed
):
    server = make_live_server(sib_config)
    updater = Updater(
        server.url,
        public_bytes(release_key),
        installed,
        current_version="0.1.0",
        platform="windows",
    )
    assert updater.check() is None
    publish(tmp_path / "downloads", release_key, b"app")
    with_downloads = make_live_server(sib_config, download_dir=tmp_path / "downloads")
    html = urllib.request.urlopen(with_downloads.url + "/").read().decode()
    assert (
        f"version {NEW_VERSION}" in html and "GunTilingSearch-9.9.9-windows.exe" in html
    )
