# Raspberry Pi setup

Target: Raspberry Pi 4/5, Raspberry Pi OS 64-bit (Bookworm or newer). Commands run on the Pi unless noted.

The design goal is that nothing on the internet can reach the Pi directly, and that even a fully
compromised Pi cannot reach your PC:

- Cloudflare Tunnel makes only outbound connections, so no router port is opened.
- The API listens on 127.0.0.1 only, as an unprivileged user in a strict systemd sandbox.
- The firewall stops the Pi from opening connections to anything on your home network except the
  router (DNS/DHCP).
- Never keep SSH keys, saved passwords or network-share mounts for your PC on the Pi.

## 1. Base system and SSH

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y unattended-upgrades nftables python3-venv
sudo dpkg-reconfigure -plow unattended-upgrades     # answer Yes: automatic security updates
```

Use SSH keys only. From your PC (not the Pi): `ssh-copy-id <user>@<pi-address>`. Then on the Pi:

```sh
sudo tee /etc/ssh/sshd_config.d/10-hardening.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sudo systemctl restart ssh
```

Check from a second terminal that you can still log in before closing the first one.

## 2. Firewall

Copy `deploy/nftables-tilingvol.conf` to the Pi, set `GATEWAY` to your router's address
(`ip route | grep default` shows it), then:

```sh
sudo mkdir -p /etc/nftables.d && sudo cp nftables-tilingvol.conf /etc/nftables.d/
echo 'include "/etc/nftables.d/*.conf"' | sudo tee -a /etc/nftables.conf
sudo nft -c -f /etc/nftables.conf && sudo systemctl enable --now nftables && sudo systemctl restart nftables
ping -c1 -W2 <your-pc-address>     # should now fail
curl -sI https://cloudflare.com | head -1   # should still work
```

SSH, VNC and local websites keep working from your PC (incoming connections from the home network),
and so do RealVNC cloud connections (outgoing). VNC from the Pi to your PC is blocked on purpose.
Never expose port 5900 to the internet or through the tunnel.

## 3. Service user and code

On your PC, package the code (no git push needed):

```sh
git archive -o tilingvol.tar.gz HEAD
scp tilingvol.tar.gz <user>@<pi-address>:
```

On the Pi:

```sh
sudo useradd --system --home /var/lib/tilingvol --shell /usr/sbin/nologin tilingvol
sudo mkdir -p /opt/tilingvol/app /var/lib/tilingvol/downloads /etc/tilingvol
sudo tar -xzf ~/tilingvol.tar.gz -C /opt/tilingvol/app
sudo chown -R tilingvol:tilingvol /var/lib/tilingvol
sudo chmod 700 /var/lib/tilingvol

curl -LsSf https://astral.sh/uv/install.sh | sh     # installs uv for your own user
cd /opt/tilingvol/app
sudo env "PATH=$HOME/.local/bin:$PATH" uv venv --python /usr/bin/python3 .venv
sudo env "PATH=$HOME/.local/bin:$PATH" uv sync --extra server --no-dev
```

The code and venv stay owned by root, so the service cannot modify its own code.

## 4. Configuration

```sh
sudo cp /opt/tilingvol/app/deploy/tilingvol.env.example /etc/tilingvol/tilingvol.env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste as TILINGVOL_SUBMIT_KEY
sudo nano /etc/tilingvol/tilingvol.env
sudo chown root:tilingvol /etc/tilingvol/tilingvol.env && sudo chmod 640 /etc/tilingvol/tilingvol.env
```

Keep a copy of the key on your PC in the project's `.env` (git-ignored): the client build needs it.

Import the existing results once (copy `output/rect_sweep.txt` from the research repo to the Pi first):

```sh
sudo install -o tilingvol -m 600 ~/rect_sweep.txt /var/lib/tilingvol/rect_sweep.txt
sudo -u tilingvol env TILINGVOL_ENV_FILE=/etc/tilingvol/tilingvol.env TILINGVOL_IMPORT_FILE=/var/lib/tilingvol/rect_sweep.txt /opt/tilingvol/app/.venv/bin/python -m tilingvol.server.import_sweep
```

The symmetric sweep (`output/sym_hunt3.txt`) imports the same way and becomes symmetric hunt results:

```sh
sudo install -o tilingvol -m 600 ~/sym_hunt3.txt /var/lib/tilingvol/sym_hunt3.txt
sudo -u tilingvol env TILINGVOL_ENV_FILE=/etc/tilingvol/tilingvol.env TILINGVOL_IMPORT_FILE=/var/lib/tilingvol/sym_hunt3.txt /opt/tilingvol/app/.venv/bin/python -m tilingvol.server.import_sweep
```

## 5. Run the service

```sh
sudo cp /opt/tilingvol/app/deploy/tilingvol.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tilingvol
systemctl status tilingvol
curl -s http://127.0.0.1:8000/ | head -5
systemd-analyze security tilingvol      # should report a low exposure score
```

If it fails to start, `journalctl -u tilingvol -e` shows why.

## 6. Cloudflare Tunnel

1. Buy a domain (Cloudflare Registrar is simplest) or move an existing one to Cloudflare (free plan).
2. Install cloudflared (the Cloudflare docs list the apt repository for Debian/Raspberry Pi OS).
3. Create the tunnel:

```sh
cloudflared tunnel login
cloudflared tunnel create tilingvol            # prints the tunnel ID
cloudflared tunnel route dns tilingvol tiling.<your-domain>
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<tunnel-id>.json /etc/cloudflared/
sudo cp /opt/tilingvol/app/deploy/cloudflared-config.yml /etc/cloudflared/config.yml   # fill in the ID and domain
sudo cloudflared service install
```

Only the web API goes through the tunnel. Do not add SSH to it.

Before you have a domain, `cloudflared tunnel --url http://127.0.0.1:8000` gives a temporary
`*.trycloudflare.com` address for testing; it changes on every restart, so it cannot go into the app.

Optional, in the Cloudflare dashboard: a rate-limiting rule on `/api/` and "Bot Fight Mode".

## 7. Build and publish the app

On your PC, in the project folder, `.env` holds:

```
TILINGVOL_SUBMIT_KEY=<same key as on the Pi>
TILINGVOL_SERVER_URL=https://fe70.org
```

The release-signing key was created once with `uv run python scripts/make_release_key.py`. It is at
`C:/Users/<you>/.tiling-volunteer-release/release_signing_key.pem`. Keep a backup somewhere safe and never
elease_signing_key.pem`. Keep a backup somewhere safe and never
copy it to the Pi: without it, installed apps cannot be updated; anyone who has it can push updates.

One-time setup so releases upload without sudo. The download folder belongs to your Pi user; the
service only reads it (apps check every signature, so this does not weaken anything):

```sh
sudo mkdir -p /srv/tilingvol-downloads
sudo chown "$USER:$USER" /srv/tilingvol-downloads && sudo chmod 755 /srv/tilingvol-downloads
sudo sed -i 's|^TILINGVOL_DOWNLOAD_DIR=.*|TILINGVOL_DOWNLOAD_DIR=/srv/tilingvol-downloads|' /etc/tilingvol/tilingvol.env
sudo systemctl restart tilingvol
```

and add `TILINGVOL_PI_SSH=you@raspberrypi.local` to the PC's `.env`.

For every release:

1. Raise `__version__` in `src/tilingvol/__init__.py` (and `version` in `pyproject.toml`).
2. `uv run python scripts/publish.py`: builds, signs, uploads to the Pi (manifest last) and checks that
   `https://fe70.org/api/update` announces the new version.

To update the server code itself (after pulling new commits): `uv run python scripts/deploy_server.py`
(asks for the Pi's sudo password once).

The public page then offers the new version, and running apps download it within 15 minutes, verify the
signature and checksum, and show "Restart now". Linux and macOS builds must be made on those systems
(`.github/workflows/build-client.yml`); put them into `release/` and run `release.py` again with
`TILINGVOL_SKIP_BUILD=1` so they are signed too.

## Settings worth knowing

Edit `/etc/tilingvol/tilingvol.env`, then `sudo systemctl restart tilingvol`:

- **Access tokens** (`TILINGVOL_ACCESS_TOKENS`): to let only people you invite run sizes, put one token per
  person there (comma-separated) and give each person theirs; removing a token locks that person out.
- **Border stages**: by default adaptive (start at the depth that settled the nearest decided size, then
  +2, +4, then full). The step, number of steps and time limit are in the same file.
- `TILINGVOL_RETRY_TIMED_OUT=1` also hands out sizes that timed out before.

## Updating

Repeat step 3's `git archive` / `scp` / `tar` (extract over the old code), run `uv sync` again, then
`sudo systemctl restart tilingvol`. The database in `/var/lib/tilingvol` is untouched.

`scripts/deploy_server.py` does all of this, and also installs the backups below.

## Backups

`tilingvol-backup.timer` copies the database every hour into `/var/backups/tilingvol`
(`tilingvol-YYYYMMDD-HH.sqlite3`, UTC). Each copy is a consistent SQLite online backup, checked with
`PRAGMA integrity_check` before it is kept. It keeps the newest 48 hourly copies plus one per day for 30
days. The job runs in the same sandbox as the server, with no network.

- See the backups: `ls -lh /var/backups/tilingvol`, and the runs: `journalctl -u tilingvol-backup -n 20`.
- Restore: `sudo systemctl stop tilingvol`, then
  `sudo install -o tilingvol -g tilingvol -m 600 /var/backups/tilingvol/<copy> /var/lib/tilingvol/tilingvol.sqlite3`,
  delete the old `tilingvol.sqlite3-wal` and `-shm` files next to it, and `sudo systemctl start tilingvol`.

Copies on the Pi do not survive a dead SD card, so also pull them to your PC. On the PC:
`uv run python scripts/pull_backup.py` copies the newest one to `output/backups` (keeps 72; log in `output/backups/pull.log`). It needs an
SSH key that logs in without a prompt. To run it every hour, in a Windows terminal:

```
schtasks /Create /SC HOURLY /TN "fe70 backup pull" /TR "cmd /c cd /d D:\code\python\tiling-volunteer && uv run python scripts/pull_backup.py"
```

The PC connects to the Pi, never the other way round, and the copy is only read as data.
