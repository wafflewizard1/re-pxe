# re-pxe

A PXE server and workshop portal for inventory, disk erasure and Windows deployment.
This is highly WIP and has only worked on a couple setups so far.
This is intended for tech shops or ITAD workplaces.
You can make routines to automate what steps and actions pcs/laptops will run,
For example: Boot -> Wipe drive -> if succeed install windows else fail and halt.

## Install

Use an x86-64 Linux server with Docker Engine, Docker Compose v2, Python 3 and a
static LAN address. Run as a user with Docker access and uid 1000. Keep this service
on your workshop LAN. Allow TCP 8080, 8443 and 8089, and UDP 69 and 30000–30031
from the client network. Keep the existing network DHCP server.

```sh
git clone https://github.com/wafflewizard1/re-pxe.git
cd re-pxe
python3 deploy/setup.py --address YOUR_SERVER_IP
```

The first build fetches and verifies pinned upstream ShredOS and the matching
sources for the bundled nwipe engine. Subsequent boots use local assets. Setup
creates independent credentials in `deploy/.env`, starts the services, copies only
the gateway’s public CA certificate and builds the UEFI iPXE loader for your address.
It preserves existing data and credentials when run again.

Open `https://YOUR_SERVER_IP:8443` or `http://YOUR_SERVER_IP:8080`. Sign in initially as `admin` with password `admin`, then change the password
in Settings → Users. Trust `data/config/server-public-ca.crt` on your
administration workstation to remove the built-in gateway’s certificate warning.
Configure DHCP’s network boot server to this server and the UEFI boot filename
to **`pxe/snponly.efi`**. Create your routine in the portal before booting clients.

Windows routines also need a Windows image, locally built WinPE media and the
native client. See [Windows setup](docs/windows.md). No Windows WIM, driver package,
private certificate, credentials, database, report or historical build is included.

For Windows network shares, install Samba on the server, then run
`python3 deploy/setup.py --address YOUR_SERVER_IP --samba`. This adds dedicated
read-only image/driver shares and a separate writable capture share. Existing
Samba configuration is backed up and validated before reload.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r server/requirements-dev.txt
# Requires libarchive-tools; packages the pinned media used by integration checks.
.venv/bin/python server/prepare_builtin.py
PYTHONPATH=server .venv/bin/pytest server/tests
node server/tests/dashboard.mjs
```

Local server tests use temporary regular files. The media packaging command runs without Docker and creates only regular files.
The generated kernel
and full corresponding source archive are ignored by Git. Native client build:
`python3 tools/build_client.py` (Docker); artifacts appear under `.local/client`.

## Operation

- Routines manage boot choices, countdowns, disk policy, erasure, Windows and Finish.
- Dashboard and Jobs show activity and every-stage controls. Reports retains failed
  and cancelled runs as well as completed jobs.
- Files provides administrator access to all regular files and folders under the
  server’s data directory, including configuration and published media.
- Database snapshots are available in Settings. Back up the entire data directory
  separately to retain images, drivers, media and evidence.
- Containers restart automatically. Run setup again to rebuild an updated source checkout.
  Active RAM clients retain their original immutable release and credentials.

[Third-party notices](THIRD_PARTY_NOTICES.md) describe the included engine and source
packaging. Microsoft ADK/WinPE and Windows installation media are supplied separately
by the operator under Microsoft’s terms.
