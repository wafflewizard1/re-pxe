# Update from the portal

Docker containers can be replaced with freshly built versions while keeping
the data directory and gateway certificate volumes. **Settings → Server → Software
updates** provides **Check for updates**, **Update now** and interrupted-update
recovery. Only administrators can use these controls.

## Enable updates once on an existing installation

Merge this feature into GitHub `main`, then bring the installation checkout up
to that version. From a clean checkout on the Linux Docker server, as the normal
installation user (uid 1000), run:

```sh
git pull --ff-only
python3 deploy/setup.py --address YOUR_SERVER_IP --portal-updates
```

Keep the existing server address. This installs a systemd service using sudo;
later checks and updates run from the portal. The host needs Python 3.12 or
newer, Git, Docker Engine, Compose v2 with `--wait`, and systemd. The installation
user must already have Docker access and be able to fetch the official GitHub
repository without an interactive login. Docker and GitHub network access are
needed while building. Initial setup may take several minutes.

If setup already published `pxe/snponly.efi` and then failed because
`re-pxe-updater.service` was not loaded or had a bad unit file setting, pull
the installer fixes and retry just the updater installation from that same
checkout:

```sh
git pull --ff-only
sudo python3 deploy/install_updater.py --root "$PWD" --user "$(id -un)"
```

Run these commands as the normal installation user. The installer accepts a
missing previous service on first installation and still requires the new
service to start successfully.

The portal runs without the Docker socket, host shell access or sudo. Its
separate control mount exposes only the updater's local socket and request drain
lock. The host service accepts checks, a confirmed pinned update, and recovery.
It always fetches `wafflewizard1/re-pxe` `main`; the browser cannot choose a URL,
branch, shell command, Compose file or filesystem path.

## Use the button

1. Open **Settings → Server**, then **Check for updates**. Review the installed
   and available revisions and the new commit's subject.
2. Finish or explicitly cancel machine jobs and uploads using their existing
   controls. The updater never cancels them for you. Uncertain erasure outcomes
   and active child workflows still block updates.
3. Choose **Update now** and confirm the displayed revision and brief service
   interruption. Leave the page open to follow progress; it reconnects after
   the restart. Reloading the page does not cancel an accepted update.

The host builds from an immutable archive of the selected commit while current
services continue running. It fetches all required images before downtime. At
the restart boundary it holds new API/PXE requests, drains existing requests and
their background tasks, and checks jobs, sessions, boot tickets and uploads
again. A job that started during the build prevents the restart.

The updater retains old container image tags, a resolved Compose deployment,
release sources and a verified SQLite backup before starting the new containers.
Compose startup checks and the API/HTTP gateway must report the selected
revision. A startup failure restores the previous containers and database.
Failed database files and all release/recovery directories are kept as evidence.
Images, drivers, reports and configured data/CA volumes are reused. Windows
client/WinPE media is not built or published by this button. The bundled ShredOS
companion follows its existing startup preparation rules after maintenance ends.

Local source edits are refused and preserved. The updater never resets or pulls
the operator's checkout; each deployed release lives in a private directory
under `.local/updater/releases`. The installed revision displayed in the portal
is recorded separately from that checkout's Git HEAD. Compatible changes to the
six existing Compose services are deployed together. A release requiring a new
host updater protocol or service layout requires manual host setup.

## Recovery and host maintenance

If an update is interrupted, the portal keeps maintenance enabled until
**Recover previous version** succeeds. Do not remove the maintenance marker to
force new deployments. If the portal itself cannot start, run on the server:

```sh
sudo systemctl stop re-pxe-updater
python3 deploy/update_agent.py --root "$PWD" --recover
sudo systemctl start re-pxe-updater
```

Run the Python command as the installation's normal Docker user. Inspect service
logs with `journalctl -u re-pxe-updater`. The command log, resolved Compose files
and database recovery copies in `.local/updater` are private and can contain
credentials and report evidence; do not publish them. A bad recovery backup or
failed rollback keeps maintenance enabled for operator inspection.

The update backup is a database recovery copy, not a complete off-server backup
of Windows images, drivers or certificates held in other volumes. Keep the
existing independent backup policy. Retained releases and image tags use storage;
do not delete the current or recovery deployment files or tags.

Before a later manual host reconfiguration or updater upgrade, use a clean
checkout of the intended GitHub main revision and rerun setup with
`--portal-updates`. Setup rebuilds that checkout and establishes it as the new
updater baseline, retaining previous release and recovery files.
