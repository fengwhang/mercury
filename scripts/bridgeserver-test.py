import os, sys, json, time, threading, urllib.request, shutil, pathlib
# HERMES SIDE of the approval bridge: start _ApprovalBridgeServer, then hit it
# like a headless omp child would (Bun fetch with unix: socket).
sys.path.insert(0, '/opt/data/home/Documents/mercury/hermes')
os.chdir('/opt/data/home/Documents/mercury/hermes')

from tools.omp_delegation import _ApprovalBridgeServer

# user callback that RECORDS the prompt and approves
seen = []
def cb(message, **kw):
    seen.append(message)
    return True
from tools import terminal_tool
terminal_tool.set_approval_callback(cb)

server = _ApprovalBridgeServer(cb)
sock_path = server.start()
print('bridge listening:', sock_path)

# Bun-style HTTP over unix socket — emulate with http.client over a socket
import http.client
class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost')
        self._path = path
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._path)
        self.sock = s
import socket

conn = UnixConnection(sock_path)
body = json.dumps({
    'kind': 'select',
    'title': 'Allow tool: bash\n[Reason: run a command]\nCommand: echo hello-from-child',
}).encode()
conn.request('POST', '/approve', body=body, headers={'Content-Type': 'application/json'})
resp = conn.getresponse()
data = json.loads(resp.read())
print('child got:', data)
assert data['value'] in ('Approve', 'Deny'), data
assert seen, 'guard stack never saw the prompt'
print('guard stack saw prompt containing command:', 'echo hello-from-child' in seen[0])
server.stop()
print('PASS: headless child approval -> hermes guard stack -> answer returned')
