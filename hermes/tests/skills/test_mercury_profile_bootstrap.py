"""Run the video skill's generated profile setup in a temporary installation."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "optional-skills" / "creative" / "kanban-video-orchestrator" / "scripts" / "bootstrap_pipeline.py"


def test_generated_pipeline_creates_own_persona_and_preserves_engine_config(tmp_path):
    spec = importlib.util.spec_from_file_location("mercury_pipeline_bootstrap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / ".mercury-nightly"
    (root / "hermes").mkdir(parents=True)
    config = {"models": {"default": "test/model"}, "hermes": {"model": {"provider": "test", "default": "model"}},
              "approvals": {"mode": "smart"}, "omp": {"tools": {"approvalMode": "write"}}}
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    (root / ".env").touch()
    plan = {"title": "Test production", "slug": "test-production", "tenant": "test-production", "scenes": [],
            "team": [{"profile": "director", "role": "director", "toolsets": ["file"], "skills": ["grill-me"],
                      "responsibilities": "A unique test persona."}]}
    setup = tmp_path / "setup.sh"
    setup.write_text(module.render_setup_sh(plan, "A test brief.", "A test team."))
    # Only kanban delivery is stubbed; profile creation and prompt seeding use
    # the real fork code. No provider calls or live service operations occur.
    cli = tmp_path / "mercury-nightly"
    cli.write_text(f"#!{sys.executable}\n" + '''import os,sys
from mercury_cli import profiles
args=sys.argv[1:]
if args[:2]==["config", "env-path"]:
    print(os.path.join(os.environ["MERCURY_HOME"], ".env"))
elif args[:2]==["profile", "show"]:
    sys.exit(0 if profiles.get_profile_dir(args[2]).is_dir() else 1)
elif args[:2]==["profile", "create"]:
    profiles.create_profile(args[2], clone_config=True, no_alias=True)
elif args and args[0]=="kanban":
    print("Temporary kanban sink")
else:
    raise SystemExit("Unexpected CLI invocation")
''')
    cli.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "MERCURY_HOME": str(root), "HERMES_HOME": str(root / "hermes"),
           "MERCURY_CONFIG": str(root / "config.yaml"), "MERCURY_CMD": str(cli), "PYTHONPATH": str(ROOT),
           "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(setup)], env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    profile = root / "hermes" / "profiles" / "director"
    assert "A unique test persona." in (profile / "config" / "SOUL.md").read_text()
    actual = yaml.safe_load((profile / "config.yaml").read_text())
    assert actual["models"] == config["models"]
    assert actual["omp"] == config["omp"]
    assert actual["approvals"] == config["approvals"]
    assert actual["hermes"]["toolsets"] == ["file"]
    assert actual["hermes"]["skills"]["always_load"] == ["grill-me"]
    assert yaml.safe_load((root / "config.yaml").read_text()) == config
