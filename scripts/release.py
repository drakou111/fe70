"""Build this OS's app, then sign every app file of the current version in release/ and write
release/latest.json. Upload the contents of release/ to the Pi's download folder afterwards.

Builds for other systems (from the GitHub workflow) can be dropped into release/ under their names
(GunTilingSearch-<version>-linux, GunTilingSearch-<version>-macos.zip) and are signed in the same run;
set TILINGVOL_SKIP_BUILD=1 to only sign. Run: uv run python scripts/release.py"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tilingvol import __version__  # noqa: E402
from tilingvol.client.release_key import RELEASE_PUBLIC_KEY_HEX  # noqa: E402
from tilingvol.client.update import current_platform, release_message  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "release"
APP_NAME = "GunTilingSearch"
SUFFIXES = {"windows": ".exe", "linux": "", "macos": ".zip"}
DEFAULT_KEY_PATH = Path.home() / ".tiling-volunteer-release" / "release_signing_key.pem"


def release_name(platform: str) -> str:
    return f"{APP_NAME}-{__version__}-{platform}{SUFFIXES[platform]}"


def load_private_key():
    path = Path(os.environ.get("TILINGVOL_RELEASE_KEY", str(DEFAULT_KEY_PATH)))
    if not path.exists():
        raise SystemExit(f"no signing key at {path}; run scripts/make_release_key.py first")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    if public != RELEASE_PUBLIC_KEY_HEX:
        raise SystemExit("the signing key does not match release_key.py; apps would reject this release")
    return key


def build_this_platform() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_client.py")], cwd=ROOT, check=True)
    platform = current_platform()
    built = ROOT / "dist" / (APP_NAME + (".exe" if platform == "windows" else ""))
    if platform == "macos":
        app = ROOT / "dist" / f"{APP_NAME}.app"
        shutil.make_archive(str(RELEASE_DIR / release_name(platform).removesuffix(".zip")), "zip", app.parent,
                            app.name)
        return
    shutil.copy2(built, RELEASE_DIR / release_name(platform))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    key = load_private_key()
    RELEASE_DIR.mkdir(exist_ok=True)
    if os.environ.get("TILINGVOL_SKIP_BUILD") != "1":
        build_this_platform()
    platforms = {}
    for platform in SUFFIXES:
        path = RELEASE_DIR / release_name(platform)
        if not path.exists():
            continue
        digest, size = sha256_of(path), path.stat().st_size
        signature = key.sign(release_message(__version__, platform, path.name, digest, size)).hex()
        platforms[platform] = {"filename": path.name, "sha256": digest, "size": size, "signature": signature}
    if not platforms:
        raise SystemExit(f"no app files for version {__version__} in {RELEASE_DIR}")
    manifest = {"version": __version__, "platforms": platforms}
    (RELEASE_DIR / "latest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    files = " ".join(f"release/{e['filename']}" for e in platforms.values())
    print(f"signed {', '.join(platforms)} for version {__version__}")
    print(f"upload: scp {files} release/latest.json <user>@<pi>:  then on the Pi:")
    print("  sudo install -o tilingvol -m 644 <files> /var/lib/tilingvol/downloads/")


if __name__ == "__main__":
    main()
