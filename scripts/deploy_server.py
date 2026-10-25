"""Update the server code on the Pi: packs the committed code (git archive), copies it over SSH, unpacks it
into /opt/tilingvol/app, refreshes the venv, restarts the service and
(re)installs the hourly backup timer. Asks for the Pi's sudo password
once. The database and settings on the Pi are untouched.
Needs TILINGVOL_PI_SSH in .env. Run: uv run python scripts/deploy_server.py"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish import ROOT, run, settings

ARCHIVE = ROOT / "build" / "tilingvol-server.tar.gz"
APP_DIR = "/opt/tilingvol/app"
BACKUP_DIR = "/var/backups/tilingvol"
BACKUP_UNITS = "deploy/tilingvol-backup.service deploy/tilingvol-backup.timer"


def main() -> None:
    remote, _, _ = settings()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    if dirty.stdout.strip():
        print(
            "note: uncommitted changes are not deployed (git archive uses the last commit)"
        )
    ARCHIVE.parent.mkdir(exist_ok=True)
    run(["git", "-C", str(ROOT), "archive", "-o", str(ARCHIVE), "HEAD"])
    run(["scp", str(ARCHIVE), f"{remote}:tilingvol-server.tar.gz"])
    remote_steps = " && ".join(
        [
            f"sudo tar -xzf ~/tilingvol-server.tar.gz -C {APP_DIR}",
            f"cd {APP_DIR}",
            'sudo env "PATH=$HOME/.local/bin:$PATH" uv sync --extra server --no-dev',
            "sudo systemctl restart tilingvol",
            "sleep 2",
            "systemctl is-active tilingvol",
            # hourly backups: a folder only the service writes and your login can read
            f'sudo install -d -o tilingvol -g "$(id -gn)" -m 2750 {BACKUP_DIR}',
            f"sudo install -m 644 {BACKUP_UNITS} /etc/systemd/system/",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable --now tilingvol-backup.timer",
            "sudo systemctl start tilingvol-backup.service",
            f"ls -lh {BACKUP_DIR} | tail -3",
            "rm ~/tilingvol-server.tar.gz",
        ]
    )
    run(["ssh", "-t", remote, remote_steps])
    print("server updated")


if __name__ == "__main__":
    main()
