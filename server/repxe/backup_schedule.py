"""Check the configured database backup schedule without machine operations."""
import logging, os, time
from pathlib import Path
from .database_recovery import backup
from .portal_updates import maintenance

if __name__ == '__main__':
    root = Path(os.environ.get('REPXE_DATA','/data'))
    while True:
        try:
            if not maintenance():
                backup(root, due=True)
        except Exception:
            logging.exception('Scheduled database backup failed')
        time.sleep(300)
