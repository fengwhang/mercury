import os, sys
os.environ['MERCURY_HOME'] = os.path.expanduser('~/.mercury')
sys.path.insert(0, '/opt/data/home/Documents/mercury/hermes')
os.chdir('/opt/data/home/Documents/mercury/hermes')
# FULL prompt build through the real entry — the exact path that crashed on the VM
import run_agent
from agent.system_prompt import build_system_prompt
import inspect
a = inspect.signature(build_system_prompt).parameters
print('params:', list(a)[:8])
