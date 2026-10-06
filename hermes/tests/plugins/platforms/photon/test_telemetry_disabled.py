"""Photon dependency telemetry cannot be enabled; startup uses a fake SDK."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from plugins.platforms.photon import cli


_SIDECAR = Path("plugins/platforms/photon/sidecar").resolve()


@pytest.mark.parametrize("inherited", ["true", "1", "yes", "on"])
def test_sidecar_never_bootstraps_dependency_telemetry(tmp_path, inherited):
    # Copy only the bundled modules: the compatibility patch must not touch an
    # installed SDK. The loader intercepts both SDK imports before any network.
    sidecar = tmp_path / "sidecar"
    sidecar.mkdir()
    for module in _SIDECAR.glob("*.mjs"):
        shutil.copy2(module, sidecar / module.name)
    loader = tmp_path / "sdk-loader.mjs"
    sdk = """
        export async function Spectrum(options) {
            const exports = [];
            if (options.telemetry) exports.push('otel.bootstrap');
            process.stdout.write(JSON.stringify({
                telemetry: options.telemetry,
                exports,
                projectId: options.projectId,
                projectSecret: options.projectSecret,
                providers: options.providers,
                flattenGroups: options.options.flattenGroups,
            }));
            process.exit(0);
        }
        export const attachment = () => {};
        export const voice = () => {};
        export const poll = () => {};
        export const text = () => {};
        export const markdown = () => {};
        export const richlink = () => {};
        export const typing = () => {};
    """
    provider = "export const imessage = {config: () => ({platform: 'imessage'})}; export const effect = {};"
    loader.write_text(
        f"const sdk = {json.dumps(sdk)};\n"
        f"const provider = {json.dumps(provider)};\n"
        "export async function resolve(specifier, context, nextResolve) {\n"
        "  if (specifier === 'spectrum-ts' || specifier === 'spectrum-ts/providers/imessage') {\n"
        "    const source = specifier === 'spectrum-ts' ? sdk : provider;\n"
        "    return {url: 'data:text/javascript,' + encodeURIComponent(source), shortCircuit: true};\n"
        "  }\n"
        "  return nextResolve(specifier, context);\n"
        "}\n"
    )
    env = {
        **os.environ,
        "PHOTON_PROJECT_ID": "provider-project",
        "PHOTON_PROJECT_SECRET": "provider-secret",
        "PHOTON_SIDECAR_TOKEN": "test-sidecar-token",
        "PHOTON_TELEMETRY": inherited,
    }
    run = subprocess.run(
        ["node", "--experimental-loader", str(loader), str(sidecar / "index.mjs")],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert run.returncode == 0, run.stderr
    startup = json.loads(run.stdout)
    assert startup["telemetry"] is False
    assert startup["exports"] == []
    assert startup["projectId"] == "provider-project"
    assert startup["projectSecret"] == "provider-secret"
    assert startup["providers"] == [{"platform": "imessage"}]
    assert startup["flattenGroups"] is True


def test_cli_rejects_removed_telemetry_command():
    parser = argparse.ArgumentParser(prog="mercury photon")
    cli.register_cli(parser)
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["telemetry", "on"])
    assert error.value.code == 2


def test_setup_manifest_has_no_telemetry_setting():
    manifest = yaml.safe_load(Path("plugins/platforms/photon/plugin.yaml").read_text())
    assert "PHOTON_TELEMETRY" not in {
        setting["name"] for setting in manifest["optional_env"]
    }
