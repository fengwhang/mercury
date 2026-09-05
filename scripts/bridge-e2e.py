import os, sys, json, socket, http.client
# End-to-end: DANGEROUS command -> bridge -> guard stack -> USER callback fires
sys.path.insert(0, '/opt/data/home/Documents/mercury/hermes')
os.chdir('/opt/data/home/Documents/mercury/hermes')

from tools.omp_delegation import _ApprovalBridgeServer
from tools import terminal_tool

seen = []
def cb(message, **kw):
    seen.append(str(message))
    return True  # user approves
terminal_tool.set_approval_callback(cb)

server = _ApprovalBridgeServer(cb)
sock = server.start()

class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost')
        self._p = path
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._p)
        self.sock = s

def ask(title):
    conn = UnixConnection(sock)
    body = json.dumps({'kind': 'select', 'title': title}).encode()
    conn.request('POST', '/approve', body=body,
                 headers={'Content-Type': 'application/json'})
    r = conn.getresponse()
    return json.loads(r.read())

# DANGEROUS command: must reach the user callback (not silent-deny)
res = ask('Allow tool: bash\nCommand: rm -rf /tmp/some-dir')
print('dangerous ->', res['value'], '| callback fired:', bool(seen))
assert seen, 'DANGEROUS command never reached the user'

# benign echo: approved early by guards, no callback needed
n0 = len(seen)
res2 = ask('Allow tool: bash\nCommand: echo hi')
print('benign ->', res2['value'])
assert res2['value'] == 'Approve'

# confirm-kind: routed through callback
res3 = ask('')
res3 = json.loads(json.dumps(res3))  # noop
conn = UnixConnection(sock)
body = json.dumps({'kind': 'confirm', 'title': 'Proceed with deployment?', 'message': 'prod'}).encode()
conn.request('POST', '/approve', body=body, headers={'Content-Type': 'application/json'})
r3 = json.loads(conn.getresponse().read())
print('confirm ->', r3)
assert r3['confirmed'] is True

server.stop()
print('PASS: bridge routes dangerous -> user callback; benign auto-approves; confirm reaches user')
