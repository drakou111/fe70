"""Server settings, read from the environment (systemd EnvironmentFile on the Pi, or a local .env file)."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from tilingvol.shared.pieces import PieceSpec, piece_by_name
from tilingvol.shared.protocol import Stage

MIN_TOKEN_LENGTH = 16
DEFAULT_STAGES = (
    Stage(20, True, 10800.0),
    Stage(26, True, 10800.0),
    Stage(0, False, 10800.0),
)


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader; existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class ServerConfig:
    db_path: Path
    submit_key: bytes
    piece: PieceSpec
    stages: tuple[Stage, ...] = DEFAULT_STAGES
    request_max_side: int = 400
    # no heartbeat for this long, or paused/stopped for idle_lease_s: the job returns to the pool
    lease_timeout_s: float = 600.0
    idle_lease_s: float = 86400.0
    heartbeat_interval_s: float = 60.0
    battle_share: float = 0.5
    near_radius: int = 8
    outlier_factor: float = 3.0
    retry_timed_out: bool = False
    # an "impossible" this much faster than the trend is flagged for re-checking
    fast_flag_factor: float = 0.1
    max_active_jobs_per_ip: int = 16
    max_requests_per_minute: int = 30
    min_client_version: str = "0.1.0"
    download_dir: Path | None = None
    # cloudflared on the same host sets CF-Connecting-IP
    trust_proxy_header: bool = True
    # adaptive borders: start at the depth that settled the nearest decided size, then go
    # border_increments steps of border_step deeper, then the full model; otherwise use `stages`
    adaptive_borders: bool = False
    default_border: int = 20
    border_step: int = 2
    border_increments: int = 2
    stage_time_limit_s: float = 10800.0
    # if not empty, every job request/heartbeat/release/result must carry one of these tokens
    access_tokens: frozenset[str] = frozenset()

    @staticmethod
    def from_env() -> "ServerConfig":
        load_dotenv(Path(os.environ.get("TILINGVOL_ENV_FILE", ".env")))
        key = os.environ.get("TILINGVOL_SUBMIT_KEY")
        if not key:
            raise RuntimeError("TILINGVOL_SUBMIT_KEY (hex) is not set")
        submit_key = bytes.fromhex(key)
        if len(submit_key) < 16:
            raise RuntimeError(
                "TILINGVOL_SUBMIT_KEY must be at least 16 bytes (32 hex digits)"
            )
        stages = DEFAULT_STAGES
        explicit_stages = bool(os.environ.get("TILINGVOL_STAGES"))
        if explicit_stages:
            stages = tuple(
                Stage(int(s["border"]), bool(s["lookahead"]), float(s["time_limit_s"]))
                for s in json.loads(os.environ["TILINGVOL_STAGES"])
            )
        if not stages or stages[-1].border != 0:
            raise RuntimeError("the last stage must be the full model (border 0)")
        download = os.environ.get("TILINGVOL_DOWNLOAD_DIR")
        tokens = frozenset(
            t.strip()
            for t in os.environ.get("TILINGVOL_ACCESS_TOKENS", "").split(",")
            if t.strip()
        )
        if any(len(t) < MIN_TOKEN_LENGTH for t in tokens):
            raise RuntimeError(
                f"access tokens must be at least {MIN_TOKEN_LENGTH} characters"
            )
        adaptive = (
            os.environ.get(
                "TILINGVOL_ADAPTIVE_BORDERS", "0" if explicit_stages else "1"
            )
            == "1"
        )
        return ServerConfig(
            db_path=Path(os.environ.get("TILINGVOL_DB", "tilingvol.sqlite3")),
            submit_key=submit_key,
            piece=piece_by_name(os.environ.get("TILINGVOL_PIECE", "gun")),
            stages=stages,
            battle_share=float(os.environ.get("TILINGVOL_BATTLE_SHARE", "0.5")),
            retry_timed_out=os.environ.get("TILINGVOL_RETRY_TIMED_OUT", "0") == "1",
            download_dir=Path(download) if download else None,
            adaptive_borders=adaptive,
            default_border=int(os.environ.get("TILINGVOL_DEFAULT_BORDER", "20")),
            border_step=int(os.environ.get("TILINGVOL_BORDER_STEP", "2")),
            border_increments=int(os.environ.get("TILINGVOL_BORDER_INCREMENTS", "2")),
            stage_time_limit_s=float(
                os.environ.get("TILINGVOL_STAGE_TIME_LIMIT_S", "10800")
            ),
            access_tokens=tokens,
        )
