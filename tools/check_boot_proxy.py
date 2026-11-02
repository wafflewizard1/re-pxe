"""Verify PXE proxy recovery after API replacement on an isolated Docker network."""
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
API = '''import http.server,json,sys
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        body=json.dumps({'generation':sys.argv[1],'path':self.path}).encode()
        self.send_response(200)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''


def docker(*args):
    result = subprocess.run(['docker', *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def wait_for(base, path, generation):
    deadline = time.monotonic() + 25
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base+path, timeout=2) as response:
                actual = json.load(response)
            assert actual == {'generation': generation, 'path': path}, actual
            return
        except (OSError, urllib.error.HTTPError, AssertionError) as exc:
            last = exc
            time.sleep(.25)
    raise AssertionError(f'PXE proxy did not reach {generation}: {last}')


def check():
    prefix = 'repxe-proxy-check-' + uuid.uuid4().hex[:12]
    network = prefix + '-net'
    containers = []
    docker('network', 'create', network)
    try:
        def api(generation):
            name = prefix + '-' + generation
            containers.append(name)
            docker('run', '-d', '--name', name, '--network', network, '--network-alias', 'api',
                   'python:3.14-slim-bookworm', 'python', '-u', '-c', API, generation)
            return name

        first = api('before')
        proxy = prefix + '-boot'
        containers.append(proxy)
        docker('run', '-d', '--name', proxy, '--network', network, '-p', '127.0.0.1::80',
               '-v', str(ROOT/'deploy/nginx-boot.conf')+':/etc/nginx/conf.d/default.conf:ro',
               'nginx:1.28-alpine')
        binding = json.loads(docker('inspect', '-f', '{{json .NetworkSettings.Ports}}', proxy))['80/tcp'][0]
        base = 'http://127.0.0.1:' + binding['HostPort']
        paths = ['/pxe/boot.ipxe?mac=52:54:00:12:34:56&windows_only=1',
                 '/pxe/managed.ipxe?mac=52:54:00:12:34:56',
                 '/pxe/routines/menu.ipxe?mac=52:54:00:12:34:56&idle=1&windows_only=1',
                 '/pxe/config/synthetic-ticket']
        for path in paths:
            wait_for(base, path, 'before')
        started = docker('inspect', '-f', '{{.State.StartedAt}}', proxy)
        ip_format = '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
        before = docker('inspect', '-f', ip_format, first)
        # Start the replacement while the old endpoint still owns its address,
        # ensuring that the API's DNS address really changes in this test.
        second = api('after')
        after = docker('inspect', '-f', ip_format, second)
        assert before != after, (before, after)
        docker('rm', '-f', first)
        for path in paths:
            wait_for(base, path, 'after')
        assert docker('inspect', '-f', '{{.State.StartedAt}}', proxy) == started
        print(json.dumps({'pxe_proxy': 'passed', 'api_address_changed': True,
                          'paths_and_queries_preserved': len(paths), 'proxy_restart_required': False}))
    finally:
        for name in reversed(containers):
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        docker('network', 'rm', network)


if __name__ == '__main__':
    check()
