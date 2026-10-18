"""The result-signing key. Release builds embed it in a generated, git-ignored module (_k.py, written by
scripts/build_client.py); development runs read TILINGVOL_SUBMIT_KEY (hex) from the environment."""

import os


def load_submit_key() -> bytes:
    try:
        from tilingvol.client import _k  # type: ignore[attr-defined]

        return _k.unpack()
    except ImportError:
        pass
    value = os.environ.get("TILINGVOL_SUBMIT_KEY")
    if not value:
        raise RuntimeError(
            "no signing key: build with scripts/build_client.py or set TILINGVOL_SUBMIT_KEY"
        )
    return bytes.fromhex(value)
