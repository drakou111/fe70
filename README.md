# fe70

A volunteer search for a rectangle tiled by the gun polyomino B(1.2^3.1^3), live at https://fe70.org. A
small server (meant for a Raspberry Pi behind a Cloudflare Tunnel) hands out work; a desktop app runs it
(enter a name, press Start) and reports back. The work is rectangle sizes (CP-SAT border and full
searches), half-strip widths (a depth that cannot be filled rules out every rectangle with that side),
fill-forever checks, and exact width decisions (`native/exact_width.cpp`: follows every filling of a
width's half-strip row by row as one automaton until it dies, tiles a rectangle or repeats). Tilings
reported as possible are verified by the server.

## Setup

```sh
uv sync --extra client --extra server
```

Create `.env` (git-ignored) with `TILINGVOL_SUBMIT_KEY=<64 hex digits>` and
`TILINGVOL_SERVER_URL=<server address>`.

## Usage

- Server (locally): `uv run uvicorn --factory tilingvol.server.app:create_app --host 127.0.0.1 --port 8000`
- App from source: `uv run python -m tilingvol.client`
- Build the app for this OS: `uv run python scripts/build_client.py` (output in `dist/`; needs `g++` for the
  exact width program in `native/`, MinGW-w64 on Windows)
- Publish a signed release (self-update): `uv run python scripts/release.py`, see `deploy/PI_SETUP.md` step 7
- Tests: `uv run pytest`
- Raspberry Pi deployment and hardening: `deploy/PI_SETUP.md`

## License

MIT, see `LICENSE`.
