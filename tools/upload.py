#!/usr/bin/env python3
"""Resumable standard-library upload client; use a trusted HTTPS CA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.request


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('file', type=Path)
    p.add_argument('--server', required=True)
    p.add_argument('--ca', type=Path, help='re-pxe root CA certificate')
    p.add_argument('--kind', choices=['image','drivers'], required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--make', default='')
    p.add_argument('--model', default='')
    p.add_argument('--windows-version', default='')
    p.add_argument('--boot-critical-inf', action='append', default=[], help='Reviewed storage/network INF archive path to service before first boot; repeat for each INF')
    p.add_argument('--reference-preparation', default='')
    p.add_argument('--image-index', type=int, default=1)
    p.add_argument('--resume', help='Existing upload ID printed by an earlier run')
    args = p.parse_args()
    if args.boot_critical_inf and args.kind != 'drivers':
        p.error('--boot-critical-inf requires a driver archive')
    token = os.environ.get('REPXE_TOKEN')
    if not token:
        p.error('Set REPXE_TOKEN to your capture, agent or administrator token')
    if not args.server.startswith('https://'):
        p.error('Use HTTPS for remote credentials')
    context = ssl.create_default_context(cafile=str(args.ca) if args.ca else None)

    def request(path, method='GET', body=None, offset=None):
        headers = {'Authorization':'Bearer '+token}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        if offset is not None:
            headers['Upload-Offset'] = str(offset)
        req = urllib.request.Request(args.server.rstrip('/')+'/api'+path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=context, timeout=3700) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f'API {exc.code}: {exc.read().decode()}') from None

    print('Computing SHA-256…', flush=True)
    with args.file.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    spec = {'kind':args.kind,'name':args.name,'filename':args.file.name,'size':args.file.stat().st_size,
            'sha256':sha,'make':args.make,'model':args.model,'windows_version':args.windows_version,
            'reference_preparation':args.reference_preparation,'image_index':args.image_index,
            'boot_critical_inf_paths':args.boot_critical_inf}
    upload = request('/uploads/'+args.resume) if args.resume else request('/uploads','POST',spec)
    if any(upload['spec'][key] != spec[key] for key in ('sha256','size','kind')):
        p.error('Resume file does not match existing upload')
    if args.boot_critical_inf and upload['spec'].get('boot_critical_inf_paths',[]) != args.boot_critical_inf:
        p.error('Resume boot-critical selection differs from the existing upload')
    uid = upload['id']
    print(f'Upload ID: {uid} (use --resume {uid} after an interruption)', flush=True)
    if upload['status'] in ('ready','published'):
        print('Already validated: '+upload['status'])
        return
    if upload['status'] != 'receiving':
        p.error('Upload is not resumable: '+upload['status'])
    offset = upload['received']
    with args.file.open('rb') as stream:
        stream.seek(offset)
        while chunk := stream.read(8*1024*1024):
            offset = request('/uploads/'+uid+'/content','PATCH',chunk,offset)['received']
            print(f'{offset}/{spec["size"]} bytes ({offset/spec["size"]:.0%})',flush=True)
    print('Validating…',flush=True)
    result = request('/uploads/'+uid+'/finish','POST')
    print("Published in the driver library." if result["status"]=="published" else f'{result["status"]}: review the Windows image in the dashboard.')


if __name__ == '__main__':
    main()
