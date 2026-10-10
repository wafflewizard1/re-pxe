# Current nwipe engine source

Normal server installation uses the tested bundled engine; rebuilding it is not
required. The current source archives and dependency manifest are under
`server/repxe/assets/nwipe/sources`. `tools/build_nwipe_sanitize.py` and
`tools/nwipe_sanitize` contain the matching build script and extension.

To rebuild, make a new private directory under `.local`, download each Debian
binary package listed in `debian-packages.json` from deb.debian.org using its
Filename, Size and SHA256 fields, verify it and extract it with `dpkg-deb -x` into
a private `sysroot`. Save the records as `packages.json` beside that sysroot.
Install build utilities (cmake, autoconf, automake, pkg-config, patch, binutils and
a C compiler) on the build workstation. Copy the pinned nwipe/libconfig archives
from the sources folder into the private directory, then invoke the builder with
`--nwipe-archive`, `--libconfig-archive`, `--sysroot`, `--work-dir` and `--output`.
Choose fresh work/output paths. It builds from regular files, runs mocked lifecycle,
scope, identity and duration checks, and assembles a private loader/library closure.
It never opens a target disk.

A changed native engine needs fresh disposable file-backed VM acceptance before
being shipped. Retain the matching source and dependency notices with every binary.
