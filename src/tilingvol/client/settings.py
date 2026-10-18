"""User settings and results waiting to be sent, stored under ~/.tiling-volunteer."""

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from tilingvol.client.memory_guard import installed_ram_mb

DEV_SERVER_URL = "http://127.0.0.1:8000"


def default_server_url() -> str:
    """Release builds carry the public URL (scripts/build_client.py); development uses the environment."""
    try:
        from tilingvol.client import _k  # type: ignore[attr-defined]

        return _k.SERVER_URL
    except ImportError:
        return os.environ.get("TILINGVOL_SERVER_URL", DEV_SERVER_URL)


DEFAULT_RAM_FRACTION = 0.5
MAX_NAME_LENGTH = 40


def app_dir() -> Path:
    override = os.environ.get("TILINGVOL_HOME")
    path = Path(override) if override else Path.home() / ".tiling-volunteer"
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("enter a name first; it is shown next to your results")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"the name can be at most {MAX_NAME_LENGTH} characters")
    if not name.isprintable():
        raise ValueError("the name contains unprintable characters")
    return name


@dataclass(frozen=True)
class Settings:
    name: str = ""
    max_ram_mb: int = 0
    max_threads: int = 0
    server_url: str = field(default_factory=default_server_url)

    @staticmethod
    def defaults() -> "Settings":
        cores = os.cpu_count() or 2
        return Settings(
            max_ram_mb=int(installed_ram_mb() * DEFAULT_RAM_FRACTION),
            max_threads=max(1, cores - 1),
        )

    def validated(self) -> "Settings":
        total = installed_ram_mb()
        if not 256 <= self.max_ram_mb <= total:
            raise ValueError(
                f"max RAM must be between 256 MB and the installed {total} MB"
            )
        cores = os.cpu_count() or 1
        if not 1 <= self.max_threads <= cores:
            raise ValueError(f"CPU threads must be between 1 and {cores}")
        return replace(
            self, name=validate_name(self.name), server_url=self.server_url.strip()
        )


class SettingsStore:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "settings.json"

    def load(self) -> Settings:
        base = Settings.defaults()
        if not self.path.exists():
            return base
        data = json.loads(self.path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
        return replace(base, **known)

    def save(self, settings: Settings) -> None:
        data = asdict(settings)
        if data["server_url"] == default_server_url():
            del data[
                "server_url"
            ]  # follow the default, so a new build can move the server
        atomic_write_text(self.path, json.dumps(data, indent=2))


class PendingResults:
    """Finished results are written here before sending, so a server outage never loses one."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "pending_results.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def add(self, submission: dict) -> None:
        self._save(self.load() + [submission])

    def remove(self, salt: str) -> None:
        self._save([s for s in self.load() if s["salt"] != salt])

    def _save(self, items: list[dict]) -> None:
        atomic_write_text(self.path, json.dumps(items))
