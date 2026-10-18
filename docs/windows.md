# Windows client and WinPE

Windows routines need **two separate images**:

- `install.wim` is the Windows image to install. An AMD64 `install.wim` from
  Windows installation media is the right type of file; choose and review its
  edition/index in the portal. Custom captured images must be generalized offline.
- `boot.wim` is a separate WinPE environment containing the re-pxe client. Build
  it with the Microsoft ADK and publish the resulting PXE bundle below.

Uploading `install.wim` and running `deploy/setup.py` do not create WinPE. Setup
publishes the iPXE loader and includes ShredOS, so erasure-only routines can run
without Windows media. A Windows routine remains a saved draft until validated
WinPE is published. The portal does not build or publish WinPE for you.

## Build the client and WinPE

Build the current native client on a Linux Docker workstation:

```sh
python3 tools/build_client.py
```

The build runs native unit checks before cross-compiling Windows executables.
Copy `.local/client` and `client/media` to a Windows ADK build workstation. Install
the Microsoft ADK Deployment Tools and WinPE add-on using
`client/media/Install-WorkstationTools.ps1` or your existing ADK installation.

Copy the server’s **public** CA certificate to the workstation and calculate its
SHA-256 with `Get-FileHash`. Run PowerShell as administrator:

```powershell
.\client\media\Build-WinPE.ps1 `
  -Client C:\re-pxe\artifacts\re-pxe.exe `
  -Tests C:\re-pxe\artifacts\repxe_core_tests.exe `
  -WorkDirectory C:\re-pxe\WinPE-current `
  -Output C:\re-pxe\re-pxe-current-amd64.iso `
  -TrustedServerCertificate C:\re-pxe\server-public-ca.crt `
  -TrustedServerCertificateSha256 REVIEWED_SHA256
```

Choose new work/output paths for each build. This script produces file artifacts;
it does not format a USB drive or install Windows. Use normal media without lab
startup switches for deployment. Copy the PXE files from the generated work
directory (no ISO mounting is required):

```powershell
New-Item -ItemType Directory -Path C:\re-pxe\pxe-files
Copy-Item C:\re-pxe\WinPE-current\media\sources\boot.wim C:\re-pxe\pxe-files\boot.wim
Copy-Item C:\re-pxe\WinPE-current\media\Boot\BCD C:\re-pxe\pxe-files\BCD
Copy-Item C:\re-pxe\WinPE-current\media\Boot\boot.sdi C:\re-pxe\pxe-files\boot.sdi
Copy-Item C:\re-pxe\WinPE-current\media\EFI\Boot\bootx64.efi C:\re-pxe\pxe-files\bootx64.efi
```

Copy these four files into `.local/pxe` on the Linux server. Copy the ISO's
builder manifest, `re-pxe-current-amd64.iso.json`, to `.local/pxe` too. Keep the
original ISO and manifest together on the build workstation. The stock
`sources/boot.wim` from a Windows USB stick does not contain the re-pxe client.

## Publish the boot media

Stage pinned wimboot using `python3 tools/build_wimboot.py`. Build the current iPXE
loader with `python3 tools/build_pxe_bootloader.py --address YOUR_SERVER_IP` on the
server. The loader build also stages matching files, source metadata and licenses in
`.local/pxe`. Run the wimboot staging tool on the same server so this directory
contains every required publication artifact.

Run the following from the Linux checkout, using a Python virtual environment
with `tools/requirements.txt` installed. Replace `SERVER`, `USER` and
`/absolute/re-pxe/path` with the server's SSH address, SSH user and checkout path.
The checkout path is not the container's `/data` path. These commands also work
when publishing back to the same server over SSH.

```sh
python3 -m pip install -r tools/requirements.txt
python3 tools/publish_client.py \
  --host SERVER --user USER --server-directory /absolute/re-pxe/path
python3 tools/publish_pxe.py \
  --host SERVER --user USER --server-directory /absolute/re-pxe/path \
  --media-manifest .local/pxe/re-pxe-current-amd64.iso.json --stage-only
```

The helpers prompt for the SSH password and require the server's host key in
your known-hosts file. Staging verifies the server copies and retains existing
selected media. Test the normal WinPE ISO on disposable file-backed fixtures and
supported client models. After acceptance, publish the validated bundle:

```sh
python3 tools/publish_pxe.py \
  --host SERVER --user USER --server-directory /absolute/re-pxe/path \
  --media-manifest .local/pxe/re-pxe-current-amd64.iso.json --validated
```

These helpers verify artifacts and retain immutable existing releases. Do not
manually create or edit `data/boot/pxe/current.json` to bypass the media check.
Return to **Setup wizard → Components** and check that Windows PXE boot media
shows **Validated**, then publish your existing routine draft. DHCP stays on
`pxe/snponly.efi`; no new DHCP filename is required.

## Prepare the installation image and routine

Upload your generalized AMD64 Windows image in the portal, select its image index,
review its validation and publish it. Create an enabled Windows preset, then use
that preset in a Windows step of a published routine. Driver packages can be added
through the portal; exact-model reviewed drivers and Windows Update are supported.

Native capture/deployment rejects running Windows. Build on a Windows workstation;
perform capture/deployment from WinPE on an explicitly selected target. For automated
tests use only disposable file-backed VM disks, never physical host storage.
