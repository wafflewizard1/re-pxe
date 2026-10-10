#!/usr/bin/env python3
"""Build current client and tests using Docker; writes only build artifacts."""
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
subprocess.run(['docker', 'build', '-t', 're-pxe-client-build', str(ROOT/'client')], check=True)
out = ROOT/'.local/client'; out.mkdir(parents=True, exist_ok=True)
container = subprocess.check_output(['docker','create','re-pxe-client-build'],text=True).strip()
try:
    subprocess.run(['docker','cp',container+':/artifacts/.',str(out)],check=True)
finally:
    subprocess.run(['docker','rm',container],check=True)
print('Client artifacts: '+str(out))
