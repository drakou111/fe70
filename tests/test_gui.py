"""Drives the real window: fields, Start, Pause/Resume, Stop, Give Up, the update banner, close."""

import tkinter as tk

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.conftest import wait_for
from tests.test_update import public_bytes, publish
from tilingvol.client import gui
from tilingvol.client.controller import State
from tilingvol.client.gui import App


@pytest.fixture
def server(sib_config, make_live_server, tmp_path):
    return make_live_server(sib_config, download_dir=tmp_path / "downloads")


@pytest.fixture
def app(tmp_path, monkeypatch, submit_key, server):
    release_key = Ed25519PrivateKey.generate()
    publish(tmp_path / "downloads", release_key, b"new app version")
    installed = tmp_path / "app" / "fe70.exe"
    installed.parent.mkdir()
    installed.write_bytes(b"old app version")
    monkeypatch.setenv("TILINGVOL_HOME", str(tmp_path))
    monkeypatch.setenv("TILINGVOL_SUBMIT_KEY", submit_key.hex())
    monkeypatch.setenv("TILINGVOL_SERVER_URL", server.url)
    monkeypatch.setattr(gui, "FIRST_UPDATE_CHECK_S", 0.2)
    try:
        root = tk.Tk()
    except tk.TclError as error:
        if "display" not in str(error):
            raise
        pytest.skip("no display")
    window = App(
        root, update_key_hex=public_bytes(release_key).hex(), install_path=installed
    )
    yield window
    try:
        alive = bool(root.winfo_exists())
    except tk.TclError:  # already destroyed by the test
        alive = False
    if alive:
        window.close()


def pump_until(app: App, predicate, timeout_s: float):
    def step():
        app.root.update()
        return predicate()

    return wait_for(step, timeout_s, interval_s=0.05)


# one test only: the bundled Tcl cannot create a second Tk root in the same process
def test_full_button_cycle(app, server):
    live_server = server
    pump_until(app, lambda: "is ready" in app.update_var.get(), 30)
    assert app.update_button.winfo_ismapped()
    assert (
        app._install_path.parent / "fe70-9.9.9-windows.exe.download"
    ).read_bytes() == b"new app version"
    app.name_var.set("  ")
    app.start_clicked()
    assert "name" in app.error_var.get()
    assert app.state == State.IDLE
    app.name_var.set("gui tester")
    app.ram_var.set("2")
    app.threads_var.set("2")
    app.url_var.set(live_server.url)
    app.width_var.set("35")
    app.height_var.set("35")
    app.start_clicked()
    pump_until(
        app,
        lambda: (
            app.state == State.RUNNING and app.slot_rows()[0][0].startswith("35 x 35")
        ),
        60,
    )
    assert str(app.name_entry.cget("state")) == "disabled"
    app.pause_clicked()
    pump_until(
        app,
        lambda: app.state == State.PAUSED and app.pause_button.cget("text") == "Resume",
        10,
    )
    app.pause_clicked()
    pump_until(app, lambda: app.state == State.RUNNING, 10)
    app.stop_button.invoke()
    pump_until(
        app,
        lambda: (
            app.state == State.STOPPED
            and app.start_button.cget("text") == "Start again"
        ),
        10,
    )
    app.give_up_button.invoke()
    pump_until(app, lambda: app.state == State.IDLE, 10)
    assert not live_server.db.active_jobs()
    app.width_var.set("")
    app.height_var.set("")
    app.threads_var.set("4")
    app.parallel_var.set("2")
    assert app.share_var.get() == "each: 2 threads, 1.0 GB"
    app.ram_var.set("4")
    app.start_clicked()
    assert len(app.slots) == 2
    # two searches at once, on different sizes, both reporting results
    pump_until(
        app,
        lambda: len({(j.width, j.height) for j in live_server.db.active_jobs()}) == 2,
        60,
    )
    assert all(j.threads <= 2 for j in live_server.db.active_jobs())
    pump_until(
        app,
        lambda: (
            len([r for r in live_server.db.results() if r.source == "volunteer"]) >= 4
        ),
        120,
    )
    pump_until(app, lambda: app.picture_frame.winfo_ismapped(), 60)
    assert list((app.directory / "pictures").glob("*.png"))
    app.help_var.set("")
    app.stop_button.event_generate("<Enter>")
    pump_until(app, lambda: app.help_var.get().startswith("Stop & free RAM"), 5)
    log_text = app.log.get("1.0", "end")
    assert "[1] " in log_text and "[2] " in log_text
    app.close()
    assert not live_server.db.active_jobs()
    assert app.store.load().name == "gui tester"
