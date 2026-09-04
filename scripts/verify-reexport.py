import os
os.environ['MERCURY_HOME'] = os.path.expanduser('~/.mercury')
import sys
sys.path.insert(0, '/opt/data/home/Documents/mercury/hermes')
os.chdir('/opt/data/home/Documents/mercury/hermes')
import run_agent
assert hasattr(run_agent, 'load_agents_md_home'), 'still missing'
assert hasattr(run_agent, 'load_project_memory_hint'), 'still missing'
assert hasattr(run_agent, 'load_soul_md') and hasattr(run_agent, 'load_hermes_md_home')
print('run_agent re-exports: all four loaders resolve')
from agent import system_prompt as sp
print('system_prompt _ra resolution:', hasattr(sp._ra(), 'load_agents_md_home'))
# full build of the stable prompt tier through the real entry
import inspect
sig = None
for name in ('build_system_prompt', 'build_stable_system_prompt', '_build_stable_parts'):
    fn = getattr(sp, name, None)
    if fn:
        sig = name
        break
print('prompt entry found:', sig)
