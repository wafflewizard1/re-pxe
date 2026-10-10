"""Initialize storage and independent secrets; preserve them on redeployment."""
from pathlib import Path
import os
import secrets
import sys

root = Path(__file__).resolve().parents[1]
data = root/'data'
for path in ('database','images/published','images/staging','drivers/packages',
             'drivers/manifests','uploads/staging','reports','boot'):
    (data/path).mkdir(parents=True,exist_ok=True)
os.chmod(data/'uploads',0o700)
os.chmod(data/'database',0o700)
os.chmod(data/'reports',0o700)
env = root/'deploy/.env'
if not env.exists():
    values = {'REPXE_ADDRESS':sys.argv[1], 'REPXE_DATA_ROOT':str(data)}
    for role in ('ADMIN','CAPTURE','AGENT','DEPLOY'):
        values['REPXE_'+role+'_TOKEN'] = secrets.token_hex(32)
    values['REPXE_SMB_DEPLOY_PASSWORD'] = secrets.token_hex(24)
    values['REPXE_SMB_CAPTURE_PASSWORD'] = secrets.token_hex(24)
    fd = os.open(env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(''.join(f'{k}={v}\n' for k,v in values.items()))
os.chmod(env,0o600)
print('Storage initialized; private deployment credentials preserved.')
