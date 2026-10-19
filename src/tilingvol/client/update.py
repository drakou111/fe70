"""Signed self-update. The server only relays files: every release entry is signed with an Ed25519 key that
lives on the maintainer's PC (scripts/release.py), and the app accepts an update only if the signature
matches the public key built into it, the file's SHA-256 matches the signed value, and the version is newer
than its own. A compromised server or tunnel therefore cannot make volunteers run anything unsigned."""

import hashlib
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tilingvol import __version__
from tilingvol.client import tls
from tilingvol.client.api import TIMEOUT_S, ApiClient, ServerError

MAX_UPDATE_BYTES = 400 << 20
CHUNK_BYTES = 1 << 16
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
VERSION = re.compile(r"\d+(\.\d+){0,3}")
SELF_REPLACING = (
    "windows",
    "linux",
)  # macOS gets a notice instead (an .app bundle cannot swap itself)


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    platform: str
    filename: str
    sha256: str
    size: int
    signature: str

    def signed_message(self) -> bytes:
        return release_message(
            self.version, self.platform, self.filename, self.sha256, self.size
        )


def release_message(
    version: str, platform: str, filename: str, sha256: str, size: int
) -> bytes:
    return f"tilingvol-release|{version}|{platform}|{filename}|{sha256}|{size}".encode()


def current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def installed_executable() -> Path | None:
    """The running app file, or None when running from source (nothing to replace)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def parse_release(manifest: dict, platform: str) -> Release | None:
    """The manifest entry for this platform, after strict format checks (not yet the signature)."""
    version = manifest.get("version")
    entry = (
        manifest.get("platforms", {}).get(platform)
        if isinstance(manifest.get("platforms"), dict)
        else None
    )
    if (
        not isinstance(version, str)
        or not VERSION.fullmatch(version)
        or not isinstance(entry, dict)
    ):
        return None
    try:
        release = Release(
            version,
            platform,
            str(entry["filename"]),
            str(entry["sha256"]).lower(),
            int(entry["size"]),
            str(entry["signature"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise UpdateError(f"malformed update manifest: {error}") from error
    if not FILENAME.fullmatch(release.filename) or not re.fullmatch(
        r"[0-9a-f]{64}", release.sha256
    ):
        raise UpdateError("malformed update manifest entry")
    if not 0 < release.size <= MAX_UPDATE_BYTES:
        raise UpdateError("update size out of range")
    return release


def verify_release(release: Release, public_key: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(release.signature), release.signed_message()
        )
    except (InvalidSignature, ValueError) as error:
        raise UpdateError(
            "the update is not signed by the project key; ignoring it"
        ) from error


class Updater:
    def __init__(
        self,
        server_url: str,
        public_key: bytes,
        install_path: Path | None,
        current_version: str = __version__,
        platform: str | None = None,
    ) -> None:
        self.api = ApiClient(server_url)
        self.public_key = public_key
        self.install_path = install_path
        self.current_version = current_version
        self.platform = platform or current_platform()

    @property
    def can_self_replace(self) -> bool:
        return self.install_path is not None and self.platform in SELF_REPLACING

    def check(self) -> Release | None:
        """A verified, newer release for this platform, or None."""
        try:
            manifest = self.api.get_json("/api/update")
        except ServerError as error:
            if error.status == 404:
                return None
            raise
        release = parse_release(manifest, self.platform)
        if release is None or version_tuple(release.version) <= version_tuple(
            self.current_version
        ):
            return None
        verify_release(release, self.public_key)
        return release

    def download_url(self, release: Release) -> str:
        return f"{self.api.base_url}/download/{release.filename}"

    def download(
        self, release: Release, progress: Callable[[float], None] | None = None
    ) -> Path:
        """Stream the file next to the app, checking size and SHA-256; returns the verified file."""
        if self.install_path is None:
            raise UpdateError("running from source; nothing to download")
        target = self.install_path.with_name(release.filename + ".download")
        digest = hashlib.sha256()
        received = 0
        request = urllib.request.Request(
            self.download_url(release),
            headers={"User-Agent": f"tilingvol/{__version__}"},
        )
        try:
            with (
                tls.urlopen(request, timeout=TIMEOUT_S) as response,
                target.open("wb") as out,
            ):
                while chunk := response.read(CHUNK_BYTES):
                    received += len(chunk)
                    if received > release.size:
                        raise UpdateError("the update is larger than announced")
                    digest.update(chunk)
                    out.write(chunk)
                    if progress:
                        progress(received / release.size)
        except (OSError, UpdateError):
            target.unlink(missing_ok=True)
            raise
        if received != release.size or digest.hexdigest() != release.sha256:
            target.unlink(missing_ok=True)
            raise UpdateError(
                "the downloaded file does not match the signed checksum; discarded"
            )
        return target

    def install(self, downloaded: Path) -> Path:
        """Swap the new file in. A running executable can be renamed (not overwritten) on Windows, so the
        old one moves aside and is deleted on the next start."""
        if not self.can_self_replace:
            raise UpdateError(
                "this platform updates by downloading the new version manually"
            )
        current = self.install_path
        old = old_path(current)
        old.unlink(missing_ok=True)
        os.replace(current, old)
        try:
            os.replace(downloaded, current)
        except OSError:
            os.replace(old, current)
            raise
        if self.platform != "windows":
            current.chmod(0o755)
        return current


def old_path(install_path: Path) -> Path:
    return install_path.with_name(install_path.name + ".old")


def remove_previous_version(install_path: Path | None) -> None:
    if install_path is None:
        return
    try:
        old_path(install_path).unlink(missing_ok=True)
    except PermissionError:
        pass  # the previous version is still shutting down; the next start removes it
