"""Opt-in Windows-only signed PXE entry; firmware trust is tested separately."""
import json
import re

from fastapi import HTTPException

DIRECTORY = r'ipxe-2\.0\.0-shim-16\.1-[a-f0-9]{12}'
WINDOWS_DIRECTORY = r'(candidate-)?[0-9]+\.[0-9]+\.[0-9]+(-[a-f0-9]{12})?'
WINDOWS_FILES = ('boot.wim', 'BCD', 'boot.sdi', 'bootx64.efi', 'wimboot')
LOADER_FILES = ('snponly-shim.efi', 'snponly.efi', 'autoexec.ipxe')


def supported(spec):
    steps = spec.get('steps', [])
    return (sum(s.get('kind') == 'windows' for s in steps) == 1
            and all(s.get('kind') in ('boot', 'windows', 'finish') for s in steps))


def require_supported(spec):
    if not supported(spec):
        raise HTTPException(409, 'This signed boot entry supports Windows-only routines. Erasure and custom boot steps need the regular PXE entry.')


def load(root):
    """Read the explicitly selected signed candidate, without changing normal media."""
    path = root / 'boot/pxe/secureboot/current.json'
    try:
        data = json.loads(path.read_text())
        if data['schemaVersion'] != 1 or data['scope'] != 'windows-only' or data['architecture'] != 'amd64':
            raise ValueError('Unsupported signed media')
        directory = data['directory']
        windows = data['windowsMedia']
        if not re.fullmatch(DIRECTORY, directory) or not re.fullmatch(WINDOWS_DIRECTORY, windows['directory']):
            raise ValueError('Invalid media directory')
        release = root / 'boot/pxe/secureboot' / directory
        if json.loads((release / 'release.json').read_text()) != data:
            raise ValueError('Signed release differs from selection')
        for base, records, names in [(release, data['files'], LOADER_FILES),
                                     (root / 'boot/pxe' / windows['directory'], windows['files'], WINDOWS_FILES)]:
            for name in names:
                item = records[name]
                if not re.fullmatch(r'[a-f0-9]{64}', item['sha256']) or type(item['size']) is not int or item['size'] <= 0:
                    raise ValueError('Invalid media metadata')
                if not (base / name).is_file() or (base / name).stat().st_size != item['size']:
                    raise ValueError('Pinned media is missing or changed')
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(503, 'Windows Secure Boot entry is unavailable; stage and select a signed candidate first') from exc


def status(root):
    try:
        data = load(root)
        return {'available': True, 'scope': 'windows-only', 'validation': 'Candidate — hardware testing required',
                'dhcp_filename': 'pxe/secureboot/' + data['directory'] + '/snponly-shim.efi',
                'windows_version': data['windowsMedia']['version']}
    except HTTPException:
        return {'available': False, 'scope': 'windows-only', 'validation': 'Not staged'}
