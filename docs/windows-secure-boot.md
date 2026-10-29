# Windows-only Secure Boot candidate

The separate signed PXE entry boots managed AMD64 WinPE while firmware Secure
Boot remains enabled. It offers published routines containing one Windows step,
with Boot and Finish nodes. Erasure, custom iPXE, inventory and checkpoint steps
are excluded from this first implementation. The regular PXE entry is preserved.

## Signed components

`tools/secure_boot_assets.json` pins the production-signed iPXE 2.0.0 package,
its corresponding source, and the newer iPXE shim 16.1 binary and source. The shim
and existing pinned wimboot 2.9.0 carry both Microsoft 2011 and 2023 signing chains.
The iPXE executable is the production-signed file from the release package. Do
not substitute the rolling build, whose iPXE executable uses a test certificate.

The bootstrap is an external `autoexec.ipxe`, because modifying the signed EFI
executables would invalidate their signatures. It uses the active firmware NIC
(`netX`) and requests the Windows-only server entry. Source archives and copyright
notices are staged alongside the signed files. No private signing keys are used.

See the [iPXE Secure Boot guide](https://ipxe.org/secboot) and the
[iPXE shim 16.1 release](https://github.com/ipxe/shim/releases/tag/ipxe-16.1).

## Stage on the server

First publish normal managed AMD64 WinPE with the existing publication tools.
From the server checkout, run:

```sh
python3 tools/stage_secure_boot.py --address YOUR_SERVER_ADDRESS --select
```

This verifies the pinned downloads and all referenced Windows media files, then
creates an immutable directory under `data/boot/pxe/secureboot`. Its manifest pins
the existing Windows media directory, so a later normal PXE publication cannot
silently switch this candidate's boot files. Re-run staging to create a candidate
for new Windows media. Cached files with different hashes are rejected.

`--select` updates only the separate signed entry's `current.json`. Omitting it
stages the files without selecting them. Neither option changes DHCP,
`pxe/snponly.efi`, normal `pxe/current.json`, presets or published routines.

The command prints a test boot filename of the form:

```text
pxe/secureboot/ipxe-2.0.0-shim-16.1-BUNDLE_HASH/snponly-shim.efi
```

Use the exact printed path for a test machine or test VLAN's DHCP boot filename.
Keep the boot server address unchanged. Settings → Routine boot settings also
shows the selected candidate's path and its testing status.

## Routine behavior

Create and publish a Windows-only routine, then assign it to the test PC or
select it in the signed boot menu. Unsupported assigned routines are not started;
the menu offers the supported alternatives. An already active erasure/custom
routine produces an explanatory hold screen without authorizing any new work.

The server binds the Windows-only restriction to both the menu ticket and the
resulting run. A different routine choice on that ticket is rejected; a started
run retains its restriction even when subsequent requests omit the query flag.
The generated menu's refresh and back links preserve the flag.
Both entries use the saved menu theme, background, text size and colours. If the
firmware cannot provide a graphical console, the menu falls back to text. The
signed menu also uses the Boot step's start delay (five seconds by default) when
a supported routine is assigned or selected as the server default. A keypress
pauses the countdown; Escape and the alternatives/back menu wait for selection.
A zero-second delay disables automatic start, and an unsupported default never
starts on the Windows-only entry. WinPE's existing drive selection and installation
countdown remain available.

These features use the upstream iPXE `console` and `choose` commands without
changing any signed executable. The production loader lacks the regular entry's
custom empty-input fix: firmware that reports phantom key events can still pause
the timer. Include unattended countdown and keyboard pause checks in physical
acceptance testing.

The WinPE re-pxe application and Windows-stage driver console are unchanged.
Secure Boot validates the EFI/kernel chain; the menu flag is a routing restriction,
not proof of firmware state. Actual Secure Boot state remains a hardware inventory
observation. Existing trusted-LAN boot transport and pinned HTTPS client trust
continue to apply.

## Acceptance testing

The signed entry has booted into re-pxe WinPE in a disposable QEMU/KVM Q35 VM
using Debian 13's EDK II 2025.02 firmware and its stock Microsoft-trusting variable
store. WinPE inventory reported `secure_boot: true`. Windows 11 Pro build 26300
then installed onto a disposable 64 GiB qcow2 disk, started in Audit mode and
opened the re-pxe driver console. `Confirm-SecureBootUEFI` in the installed OS
returned `True`. The initial lab boot order was network-only; the installed-disk
check used disk-first boot with the same firmware variable store. No custom key
enrollment was performed. Physical hardware and final OOBE remain acceptance
checks, rather than results inferred from this VM test. The older Debian 12 virtual
PXE implementation instead reported a padded 4 MiB size for the next EFI image,
which shim rejected with `Verification failed: (0x2) Invalid Parameter`. This
demonstrates why signature checks alone cannot certify a device's PXE firmware.

Firmware must trust the appropriate Microsoft UEFI authorities. Devices configured
to trust only Microsoft Windows loaders may need their vendor's third-party UEFI
trust setting enabled. Boot managers in WinPE and the installed Windows image must
also match the device's certificate/revocation state. For 2023-only Windows trust,
use appropriately updated ADK/WinPE and Windows boot files; the existing builder's
`-BootEx` and preset's BootEx option are available for supported images.
See [Microsoft's WinPE guidance](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/winpe-mount-and-customize?view=windows-11).

Test with disposable file-backed VM storage first, then Lenovo and HP test laptops:

1. Confirm Secure Boot is enabled and the signed network entry loads without key enrollment.
2. Open the Windows-only menu and reach WinPE/re-pxe.
3. Test one-drive automatic selection and multiple-drive technician selection.
4. Install Windows, finish driver restarts and complete OOBE.
5. Confirm Secure Boot remains enabled in the installed Windows inventory.
6. Confirm unsupported routines and damaged/missing candidates stop with no fallback to unsigned boot.

The manifest remains `secureBootValidated: false`; download verification and API
tests do not certify firmware compatibility. Keep this as a candidate until the
physical-machine acceptance checks are complete. Returning DHCP to the original
`pxe/snponly.efi` restores the regular boot path; staging has not modified it.
