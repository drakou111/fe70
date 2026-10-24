"""User settings and results waiting to be sent, stored under ~/.fe70.

The window edits a Form: every field is text, and an empty field means "the default" (the server's
choice for what to search, this computer's sensible share for RAM and threads). Form.resolve() turns it
into Settings, the concrete values the searches run with."""

import json
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from tilingvol.client.memory_guard import installed_ram_mb
from tilingvol.client.width_queue import parse_widths
from tilingvol.shared.protocol import (
    SEARCH_SYMMETRIC,
    SEARCH_WIDTH,
    SEARCHES,
    parse_custom_stages,
)

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
MIN_SLOT_RAM_MB = 1024
MAX_TIME_LIMIT_HOURS = 7 * 24
_PENDING_LOCK = threading.RLock()  # parallel searches share one pending file


def default_ram_mb() -> int:
    return int(installed_ram_mb() * DEFAULT_RAM_FRACTION)


def default_threads() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def app_dir() -> Path:
    override = os.environ.get("TILINGVOL_HOME")
    if override:
        path = Path(override)
    else:
        path = Path.home() / ".fe70"
        legacy = Path.home() / ".tiling-volunteer"  # the folder used by version 0.2
        if legacy.is_dir() and not path.exists():
            legacy.rename(path)
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
    # sizes searched at once; each gets 1/parallel of the threads and RAM
    parallel: int = 1
    # only for servers that require one
    access_token: str = ""
    # search every stage until it is decided instead of the server's time limit
    no_time_limit: bool = False
    # own time limit per stage (at least the server's); None: the server's
    time_limit_s: float | None = None
    # let half the threads do CP-SAT's tree search, which reports progress (may change the speed)
    tree_progress: bool = False
    # what to run: rectangle sizes ("board"), half-strip widths ("width"), or the symmetric hunt, which
    # searches only 180-degree symmetric tilings (its "impossible" rules out symmetric tilings only)
    search: str = SEARCH_WIDTH
    # own depths, e.g. "24, 28, 32": border depths (the full search follows) or strip depths. Empty: the
    # server's
    custom_stages: str = ""

    def validated(self) -> "Settings":
        total = installed_ram_mb()
        if not 256 <= self.max_ram_mb <= total:
            raise ValueError(
                f"RAM must be between 0.25 GB and the installed {total / 1024:.1f} GB"
            )
        cores = os.cpu_count() or 1
        if not 1 <= self.max_threads <= cores:
            raise ValueError(f"CPU threads must be between 1 and {cores}")
        if not 1 <= self.parallel <= self.max_threads:
            raise ValueError(
                "parallel searches must be between 1 and the number of CPU threads"
            )
        if self.max_ram_mb // self.parallel < MIN_SLOT_RAM_MB:
            raise ValueError(
                f"each parallel search needs at least {MIN_SLOT_RAM_MB // 1024} GB; raise RAM or "
                f"lower Parallel searches"
            )
        token = self.access_token.strip()
        if token and not token.isprintable():
            raise ValueError("the access token contains unprintable characters")
        if self.search not in SEARCHES:
            raise ValueError(f"unknown search {self.search!r}")
        depths = parse_custom_stages(self.custom_stages)
        return replace(
            self,
            name=validate_name(self.name),
            server_url=self.server_url.strip(),
            access_token=token,
            custom_stages=", ".join(str(d) for d in depths),
        )

    @property
    def symmetric_hunt(self) -> bool:
        return self.search == SEARCH_SYMMETRIC

    @property
    def custom_borders(self) -> tuple[int, ...]:
        return parse_custom_stages(self.custom_stages)

    def slot_settings(self) -> "Settings":
        """What one of the parallel searches may use."""
        return replace(
            self,
            max_ram_mb=self.max_ram_mb // self.parallel,
            max_threads=self.max_threads // self.parallel,
            parallel=1,
        )


@dataclass(frozen=True)
class Resolved:
    settings: Settings
    requested_size: tuple[int, int] | None  # a rectangle size (board or hunt)
    requested_widths: tuple[int, ...]  # strip widths (width search)


def parse_number(text: str, what: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{what} must be a number") from None
    if value < 0:
        raise ValueError(f"{what} cannot be negative")
    return value


def parse_whole(text: str, what: str) -> int:
    if not text.isdigit():
        raise ValueError(f"{what} must be a whole number")
    return int(text)


def parse_size(text: str) -> tuple[int, int] | None:
    """ "150 x 168", "150x168" or "150 168" -> (150, 168); empty -> None."""
    parts = text.lower().replace("x", " ").replace(",", " ").split()
    if not parts:
        return None
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("a rectangle size is width x height, like 150 x 168")
    return int(parts[0]), int(parts[1])


@dataclass(frozen=True)
class Form:
    """What the window shows; an empty field means the default."""

    name: str = ""
    search: str = SEARCH_WIDTH
    request: str = ""  # widths "149, 150" (width search) or a size "150 x 168"; empty: server picks
    stages: str = ""  # strip or border depths; empty: the server's
    time_limit_hours: str = ""  # per stage; empty: the server's, 0: none
    ram_gb: str = ""  # empty: half the installed RAM
    threads: str = ""  # empty: all cores but one
    parallel: str = ""  # empty: 1
    tree_progress: bool = False
    server_url: str = ""  # empty: the built-in server
    access_token: str = ""

    def resolve(self) -> Resolved:
        ram_mb = (
            int(parse_number(self.ram_gb.strip(), "RAM") * 1024)
            if self.ram_gb.strip()
            else default_ram_mb()
        )
        threads = (
            parse_whole(self.threads.strip(), "CPU threads")
            if self.threads.strip()
            else default_threads()
        )
        parallel = (
            parse_whole(self.parallel.strip(), "Parallel searches")
            if self.parallel.strip()
            else 1
        )
        hours = self.time_limit_hours.strip()
        limit = parse_number(hours, "The time limit") if hours else None
        if limit is not None and limit > MAX_TIME_LIMIT_HOURS:
            raise ValueError(
                f"the time limit can be at most {MAX_TIME_LIMIT_HOURS} hours"
            )
        settings = Settings(
            name=self.name,
            max_ram_mb=ram_mb,
            max_threads=threads,
            server_url=self.server_url.strip() or default_server_url(),
            parallel=parallel,
            access_token=self.access_token,
            no_time_limit=limit == 0,
            time_limit_s=limit * 3600 if limit else None,
            tree_progress=self.tree_progress,
            search=self.search,
            custom_stages=self.stages,
        ).validated()
        if self.search == SEARCH_WIDTH:
            return Resolved(settings, None, parse_widths(self.request))
        return Resolved(settings, parse_size(self.request), ())


def form_from_saved(data: dict) -> Form:
    """Settings files written before 0.11 kept resolved numbers; defaults become empty fields."""
    if "max_ram_mb" not in data:
        known = {k: v for k, v in data.items() if k in Form.__dataclass_fields__}
        return Form(**known)
    ram = data.get("max_ram_mb", 0)
    threads = data.get("max_threads", 0)
    parallel = data.get("parallel", 1)
    search = data.get("search") or (
        SEARCH_SYMMETRIC if data.get("symmetric_hunt") else SEARCH_WIDTH
    )
    return Form(
        name=data.get("name", ""),
        search=search,
        request=data.get("widths", "") if search == SEARCH_WIDTH else "",
        stages=data.get("custom_stages", ""),
        time_limit_hours="0" if data.get("no_time_limit") else "",
        ram_gb="" if not ram or ram == default_ram_mb() else f"{ram / 1024:g}",
        threads="" if not threads or threads == default_threads() else str(threads),
        parallel="" if parallel == 1 else str(parallel),
        tree_progress=bool(data.get("tree_progress", False)),
        server_url=data.get("server_url", ""),
        access_token=data.get("access_token", ""),
    )


class SettingsStore:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "settings.json"

    def load(self) -> Form:
        if not self.path.exists():
            return Form()
        return form_from_saved(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, form: Form) -> None:
        data = asdict(form)
        if data["server_url"].strip() == default_server_url():
            data["server_url"] = (
                ""  # follow the default, so a new build can move the server
            )
        atomic_write_text(self.path, json.dumps(data, indent=2))


class PendingResults:
    """Finished results are written here before sending, so a server outage never loses one."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "pending_results.json"
        self.lock = _PENDING_LOCK

    def load(self) -> list[dict]:
        with self.lock:
            if not self.path.exists():
                return []
            return json.loads(self.path.read_text(encoding="utf-8"))

    def add(self, submission: dict) -> None:
        with self.lock:
            self._save(self.load() + [submission])

    def remove(self, salt: str) -> None:
        with self.lock:
            self._save([s for s in self.load() if s["salt"] != salt])

    def _save(self, items: list[dict]) -> None:
        atomic_write_text(self.path, json.dumps(items))
