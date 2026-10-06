"""Exercise lazy Mem0 SDK startup with its import-time telemetry env contract."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["platform", "oss", "setup", "llm_import"])
@pytest.mark.parametrize("opt_in_stage", ["inherited", "after_plugin_import"])
def test_sdk_startup_telemetry_disabled_before_import(tmp_path, mode, opt_in_stage):
    sdk = tmp_path / "mem0"
    sdk.mkdir()
    # mem0 2.0.10 caches the env at import, creates a client PostHog singleton,
    # and emits client.init / mem0.init from constructors. Record those effects
    # instead of importing PostHog or contacting any provider.
    sdk.joinpath("__init__.py").write_text(
        '''import os
__version__ = "2.0.10"
enabled = os.environ.get("MEM0_TELEMETRY", "True").lower() in ("true", "1", "yes")
events = ["posthog.client_created"] if enabled else []
requests = []
class MemoryClient:
    def __init__(self, api_key):
        requests.append({"api_key": api_key})
        if enabled:
            events.append("client.init")
    def search(self, query, **kwargs):
        requests.append({"query": query, **kwargs})
        if enabled:
            events.append("client.search")
        return {"results": [{"memory": "provider fact"}]}
class Memory(MemoryClient):
    @classmethod
    def from_config(cls, config):
        requests.append({"config": config})
        return cls("oss-provider")
'''
    )
    script = '''
import json, os, sys
import plugins.memory.mem0
if sys.argv[2] == 'after_plugin_import':
    os.environ['MEM0_TELEMETRY'] = 'true'
mode = sys.argv[1]
if mode == 'setup':
    from plugins.memory.mem0._setup import _check_min_dep_version
    _check_min_dep_version()
elif mode == 'llm_import':
    # Stop at the fake SDK boundary: it intentionally lacks provider classes.
    try:
        import plugins.memory.mem0._openai_llm
    except ModuleNotFoundError as error:
        assert error.name.startswith('mem0.configs'), error
else:
    from plugins.memory.mem0._backend import PlatformBackend, OSSBackend
    if mode == 'platform':
        backend = PlatformBackend('provider-key')
    else:
        backend = OSSBackend({
            'llm': {'provider': 'ollama', 'config': {'model': 'local-model'}},
            'embedder': {'provider': 'ollama', 'config': {'model': 'custom-embedder'}},
            'vector_store': {'provider': 'qdrant', 'config': {}},
        })
    result = backend.search('tea', filters={'user_id': 'owner'}, top_k=3)
    assert result == [{'memory': 'provider fact'}], result
sdk = sys.modules.get('mem0')
print(json.dumps({
    'events': sdk.events if sdk else [],
    'requests': sdk.requests if sdk else [],
    'env': os.environ.get('MEM0_TELEMETRY'),
}))
'''
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(Path.cwd())]),
        "MEM0_TELEMETRY": "true" if opt_in_stage == "inherited" else "false",
    }
    run = subprocess.run(
        [sys.executable, "-c", script, mode, opt_in_stage],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    startup = json.loads(run.stdout)
    assert startup["events"] == []
    if mode in {"platform", "oss"}:
        assert startup["env"] == "false"
        assert startup["requests"][-1]["query"] == "tea"
        assert startup["requests"][-1]["filters"] == {"user_id": "owner"}
        if mode == "platform":
            assert startup["requests"][0] == {"api_key": "provider-key"}
        else:
            assert startup["requests"][0]["config"]["llm"]["provider"] == "ollama"
