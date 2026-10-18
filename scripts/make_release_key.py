"""Create the release-signing key pair once. The private key stays on this PC (never on the Pi, never in
git): keep a backup, because without it no installed app will accept another update. The public key is
written into src/tilingvol/client/release_key.py, which is committed.
Run: uv run python scripts/make_release_key.py   (TILINGVOL_RELEASE_KEY overrides the key location)"""

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MODULE = ROOT / "src" / "tilingvol" / "client" / "release_key.py"
DEFAULT_KEY_PATH = Path.home() / ".tiling-volunteer-release" / "release_signing_key.pem"


def key_path() -> Path:
    return Path(os.environ.get("TILINGVOL_RELEASE_KEY", str(DEFAULT_KEY_PATH)))


def main() -> None:
    path = key_path()
    if path.exists():
        raise SystemExit(f"{path} already exists; refusing to overwrite a signing key")
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    if os.name != "nt":
        path.chmod(0o600)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    text = PUBLIC_MODULE.read_text(encoding="utf-8")
    PUBLIC_MODULE.write_text(
        text.replace(
            'RELEASE_PUBLIC_KEY_HEX = ""', f'RELEASE_PUBLIC_KEY_HEX = "{public}"'
        ),
        encoding="utf-8",
    )
    print(f"private key: {path}  (back it up somewhere safe)")
    print(f"public key written to {PUBLIC_MODULE}")


if __name__ == "__main__":
    main()
