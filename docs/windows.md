# Windows client and WinPE

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
startup switches for deployment. Extract `boot.wim`, `BCD`, `boot.sdi` and
`bootx64.efi` from the generated media into `.local/pxe`. Retain its builder manifest.

Stage pinned wimboot using `python3 tools/build_wimboot.py`. Build the current iPXE
loader with `python3 tools/build_pxe_bootloader.py --address YOUR_SERVER_IP` on the
server. The loader build also stages matching files, source metadata and licenses in
`.local/pxe`. Run the wimboot staging tool on the same server so this directory
contains every required publication artifact.

Install `tools/requirements.txt` for the SSH publication helpers. Publish the native
client with `tools/publish_client.py --host SERVER --user USER --server-directory
/absolute/re-pxe/path`; publish the WinPE bundle with `tools/publish_pxe.py
--host SERVER --user USER --server-directory /absolute/re-pxe/path
--media-manifest PATH --validated`. These helpers verify artifacts and retain
immutable existing releases. Mark a release validated only after acceptance on
your disposable fixtures and supported client models.

Upload your generalized AMD64 Windows image in the portal, select its image index,
review its validation and publish it. Create an enabled Windows preset, then use
that preset in a Windows step of a published routine. Driver packages can be added
through the portal; exact-model reviewed drivers and Windows Update are supported.

Native capture/deployment rejects running Windows. Build on a Windows workstation;
perform capture/deployment from WinPE on an explicitly selected target. For automated
tests use only disposable file-backed VM disks, never physical host storage.
