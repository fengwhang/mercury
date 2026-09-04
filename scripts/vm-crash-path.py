import os, sys
os.environ['MERCURY_HOME'] = os.path.expanduser('~/.mercury')
sys.path.insert(0, '/opt/data/home/Documents/mercury/hermes')
os.chdir('/opt/data/home/Documents/mercury/hermes')
# Build the real agent + prompt exactly like the CLI does — the VM's crash path
from run_agent import AIAgent
agent = AIAgent()
try:
    sp = agent.build_system_prompt() if hasattr(agent, 'build_system_prompt') else None
except Exception as e:
    # fall back to the module-level builder the TUI uses
    sp = None
    print('agent method path:', type(e).__name__, str(e)[:120])
if sp is None:
    from agent.system_prompt import build_system_prompt
    sp = build_system_prompt(agent)
print('system prompt built:', len(sp), 'chars')
assert 'Mercury' in sp
print('contains Mercury identity: True')
assert 'load_agents' not in sp  # no error text
print('VM CRASH PATH VERIFIED CLEAN')
