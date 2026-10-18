"""Build, sign and publish a release in one go: runs scripts/release.py, uploads the app files and then
latest.json to the Pi's download folder over SSH (manifest last, so apps never see it before its file),
and checks that the public server now announces the new version.

Needs in .env: TILINGVOL_PI_SSH (e.g. you@raspberrypi.local) and TILINGVOL_SERVER_URL; optional
TILINGVOL_PI_DOWNLOADS (default /srv/tilingvol-downloads, owned by the Pi user, see deploy/PI_SETUP.md).
Run: uv run python scripts/publish.py"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "release"
DEFAULT_REMOTE_DIR = "/srv/tilingvol-downloads"
SAFE_REMOTE = re.compile(r"[A-Za-z0-9_.@-]+(@[A-Za-z0-9_.-]+)?")
SAFE_PATH = re.compile(r"/[A-Za-z0-9/_.-]+")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def settings() -> tuple[str, str, str]:
    load_dotenv(ROOT / ".env")
    remote = os.environ.get("TILINGVOL_PI_SSH", "")
    folder = os.environ.get("TILINGVOL_PI_DOWNLOADS", DEFAULT_REMOTE_DIR)
    server = os.environ.get("TILINGVOL_SERVER_URL", "")
    if not SAFE_REMOTE.fullmatch(remote):
        raise SystemExit(
            "set TILINGVOL_PI_SSH in .env, e.g. TILINGVOL_PI_SSH=you@raspberrypi.local"
        )
    if not SAFE_PATH.fullmatch(folder):
        raise SystemExit(f"unsafe TILINGVOL_PI_DOWNLOADS: {folder!r}")
    if not server.startswith("https://"):
        raise SystemExit("TILINGVOL_SERVER_URL must be the public https address")
    return remote, folder, server.rstrip("/")


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> None:
    remote, folder, server = settings()
    run([sys.executable, str(ROOT / "scripts" / "release.py")])
    manifest = json.loads((RELEASE_DIR / "latest.json").read_text(encoding="utf-8"))
    files = [
        str(RELEASE_DIR / entry["filename"]) for entry in manifest["platforms"].values()
    ]
    run(["scp", *files, f"{remote}:{folder}/"])
    run(["scp", str(RELEASE_DIR / "latest.json"), f"{remote}:{folder}/latest.json.tmp"])
    run(["ssh", remote, f"mv {folder}/latest.json.tmp {folder}/latest.json"])
    # Cloudflare's browser integrity check rejects the default Python-urllib user agent
    request = urllib.request.Request(
        server + "/api/update",
        headers={"User-Agent": f"tilingvol-publish/{manifest['version']}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        live = json.loads(response.read())
    if live.get("version") != manifest["version"]:
        raise SystemExit(
            f"uploaded, but {server} still announces {live.get('version')!r}"
        )
    print(
        f"published version {manifest['version']}; running apps pick it up within 6 hours"
    )


if __name__ == "__main__":
    main()
