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

For every release:

1. Raise `__version__` in `src/tilingvol/__init__.py` (and `version` in `pyproject.toml`).
2. `uv run python scripts/release.py`. It builds the app, signs it and writes `release/latest.json`.
3. Copy the two files to the Pi and into the download folder:

```sh
scp release/GunTilingSearch-<version>-windows.exe release/latest.json you@raspberrypi.local:
# on the Pi:
sudo install -o tilingvol -m 644 ~/GunTilingSearch-<version>-windows.exe ~/latest.json /var/lib/tilingvol/downloads/
```

The public page then offers the new version, and running apps download it within 6 hours, verify the
signature and checksum, and show "Restart now". Linux and macOS builds must be made on those systems
(`.github/workflows/build-client.yml`); put them into `release/` and run `release.py` again with
`TILINGVOL_SKIP_BUILD=1` so they are signed too.

## Updating

Repeat step 3's `git archive` / `scp` / `tar` (extract over the old code), run `uv sync` again, then
`sudo systemctl restart tilingvol`. The database in `/var/lib/tilingvol` is untouched.

Back up the database now and then: `sudo sqlite3 /var/lib/tilingvol/tilingvol.sqlite3 ".backup /home/<user>/tilingvol-backup.sqlite3"`.
