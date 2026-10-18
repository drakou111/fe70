# tiling-volunteer

A volunteer search for a rectangle tiled by the gun polyomino B(1.2^3.1^3). A small server (meant for a
Raspberry Pi behind a Cloudflare Tunnel) hands out rectangle sizes. A desktop app runs the CP-SAT
border/full search on them (enter a name, press Start) and reports each size back as impossible, timed
out, or possible with a tiling that the server verifies.

## Setup

```sh
uv sync --extra client --extra server
```

Create `.env` (git-ignored) with `TILINGVOL_SUBMIT_KEY=<64 hex digits>` and
`TILINGVOL_SERVER_URL=<server address>`.

## Usage

- Server (locally): `uv run uvicorn --factory tilingvol.server.app:create_app --host 127.0.0.1 --port 8000`
- App from source: `uv run python -m tilingvol.client`
- Build the app for this OS: `uv run python scripts/build_client.py` (output in `dist/`)
- Publish a signed release (self-update): `uv run python scripts/release.py`, see `deploy/PI_SETUP.md` step 7
- Tests: `uv run pytest`
- Raspberry Pi deployment and hardening: `deploy/PI_SETUP.md`
