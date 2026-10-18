"""Drives the real window: fields, Start, Pause/Resume, Stop, Give Up, close."""

import tkinter as tk

import pytest

from tests.conftest import wait_for
from tilingvol.client.controller import State
from tilingvol.client.gui import App


@pytest.fixture
def app(tmp_path, monkeypatch, submit_key):
    monkeypatch.setenv("TILINGVOL_HOME", str(tmp_path))
    monkeypatch.setenv("TILINGVOL_SUBMIT_KEY", submit_key.hex())
    try:
        root = tk.Tk()
    except tk.TclError as error:
        if "display" not in str(error):
            raise
        pytest.skip("no display")
    window = App(root)
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
def test_full_button_cycle(app, live_server):
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
            app.state == State.RUNNING
            and app.status_vars["Size"].get().startswith("35 x 35")
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
    app.start_clicked()
    pump_until(app, lambda: "IMPOSSIBLE" in app.log.get("1.0", "end"), 120)
    assert any(r.source == "volunteer" for r in live_server.db.results())
    app.close()
    assert not live_server.db.active_jobs()
    assert app.store.load().name == "gui tester"
