import secrets
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Self

import pytest
import uvicorn

from tilingvol.server.app import create_app
from tilingvol.server.config import ServerConfig
from tilingvol.shared.pieces import SIBLING
from tilingvol.shared.protocol import Stage

SIB_STAGES = (Stage(3, True, 60.0), Stage(0, False, 120.0))


@pytest.fixture
def submit_key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def sib_config(tmp_path: Path, submit_key: bytes) -> ServerConfig:
    return ServerConfig(
        db_path=tmp_path / "test.sqlite3",
        submit_key=submit_key,
        piece=SIBLING,
        stages=SIB_STAGES,
        heartbeat_interval_s=1.0,
        trust_proxy_header=False,
    )


def wait_for(predicate, timeout_s: float, interval_s: float = 0.1):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError(f"condition not met within {timeout_s} s")


class LiveServer:
    def __init__(self, config: ServerConfig) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.app = create_app(config)
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=self.port, log_level="warning"
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        wait_for(lambda: self._server.started, 10)
        return self

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(10)

    @property
    def db(self):
        return self.app.state.db


@pytest.fixture
def live_server(sib_config):
    with LiveServer(sib_config) as server:
        yield server


@pytest.fixture
def make_live_server():
    servers = []

    def factory(config: ServerConfig, **changes) -> LiveServer:
        server = LiveServer(replace(config, **changes)).__enter__()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.__exit__()
