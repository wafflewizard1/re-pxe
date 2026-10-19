# Third-party components

- **ShredOS**: pinned upstream release `v2025.11_31_x86-64_0.42`, from
  https://github.com/PartialVolume/shredos.x86_64. Its ISO is fetched and SHA-256
  verified during the server build; only its boot kernel enters the runtime image.
  The upstream project documents the included software and licenses.
- **nwipe**: GPL-2.0-or-later, upstream 0.43 with the re-pxe sanitize, binding and
  duration fixes. The current tested binary overlay is in
  `server/repxe/assets/nwipe/bundle.cpio.gz`. Matching upstream source, project patches,
  build script, dependency notices and pinned Debian source manifest are in
  `server/repxe/assets/nwipe/sources`. Server builds automatically fetch every
  matching dependency source by exact size and SHA-256 and create the complete
  `nwipe-source.tar.gz`. It is served beside each published ShredOS release and
  referenced from the boot overlay. No compiler/dependency binaries run on the host.
- **iPXE**: GPL-2.0-or-later, pinned commit
  `6262f1081fe185564e8ec8365a1d23597ec6e6f5` with an empty-input countdown fix.
  The build publishes the matching patched source and bootstrap beside the loader.
  License: `deploy/pxe/COPYING-ipxe`.
- **wimboot**: GPL-2.0-or-later, pinned 2.9.0. The staging tool retains its matching
  source archive and metadata. Server builds also package the verified public
  binary and corresponding source for direct uploaded-WIM boot. They are served
  at `/pxe/routines/wimboot` and
  `/pxe/routines/wimboot/wimboot-source.tar.gz`, with the license and manifest
  alongside. Upstream: https://github.com/ipxe/wimboot and https://ipxe.org/wimboot.
  License: `deploy/pxe/COPYING-wimboot`.
- **nlohmann/json** and **pugixml**: MIT licenses, retained under `client/third_party`.
- Python and container dependencies retain their upstream licenses. Server versions
  are pinned in `server/requirements.txt`; container dependencies are declared in
  the Dockerfiles and Compose file.

Microsoft software, Windows images and vendor driver packages are not distributed
in this repository. This notice does not grant a separate license to original
re-pxe project code.
