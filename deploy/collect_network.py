#!/usr/bin/env python3
"""Sample the Linux server's uplinks; never reads or enumerates disks."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time


def counters():
    result={}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name,values=line.split(':',1);name=name.strip();values=values.split()
        # Physical uplinks and common bonds. Exclude bridge/veth/container double counting.
        if name.startswith(('en','eth','wl','bond')):
            result[name]=(int(values[0]),int(values[8]))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    previous=counters();last=time.monotonic()
    while True:
        time.sleep(2);current=counters();at=time.monotonic();elapsed=at-last
        common=current.keys()&previous.keys()
        rx=sum(max(0,current[n][0]-previous[n][0]) for n in common)/elapsed
        tx=sum(max(0,current[n][1]-previous[n][1]) for n in common)/elapsed
        data={'sampled_at':datetime.now(timezone.utc).isoformat(),'interval_seconds':elapsed,
              'rx_bytes_per_second':rx,'tx_bytes_per_second':tx,
              'rx_bytes':sum(v[0] for v in current.values()),'tx_bytes':sum(v[1] for v in current.values()),
              'interfaces':sorted(current)}
        pending=args.output.with_suffix('.tmp');pending.write_text(json.dumps(data)+'\n');pending.replace(args.output)
        previous=current;last=at

if __name__=='__main__':main()
