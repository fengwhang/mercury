"""MERCURY-OMP PATCH (NB-2): `omp` subcommand — launch the omp half of Mercury.

`mercury omp [args…]` / `mercury omp [args…]` execs the patched omp build as
its own somewhat-independent program: it feels like original omp (full TUI,
all fan-out features) but with model roles structurally stripped. The model
comes from the unified config's delegate_model slot via the bridge
(fail-hard); argv after that passes through UNTOUCHED — the omp TUI is the
interface, no wrapper UI.
"""
import os
import subprocess
import shlex
import sys


def _resolve_omp_binary() -> str:
    """Locate the omp binary: HERMES_OMP_BIN, then the repo-vendored build."""
    env_bin = os.environ.get("HERMES_OMP_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    repo = os.environ.get("MERCURY_REPO", "").strip()
    if not repo:
        home = os.path.expanduser("~")
        for cand in (
            os.path.join(home, "Documents", "mercury-omp"),
            os.path.join(home, "mercury-omp"),
        ):
            if os.path.isfile(os.path.join(cand, "omp", "packages", "coding-agent", "dist", "omp")):
                repo = cand
                break
    if repo:
        vendored = os.path.join(repo, "omp", "packages", "coding-agent", "dist", "omp")
        if os.path.isfile(vendored):
            return vendored
    return ""


def cmd_omp(args) -> int:
    """Entry point for the `omp` subcommand."""
    omp_bin = _resolve_omp_binary()
    if not omp_bin:
        print(
            "omp: no omp binary found. Set HERMES_OMP_BIN or build the vendored tree\n"
            "(cd omp && bun install && bun run build:bindings && bun run build in\n"
            "packages/coding-agent).",
            file=sys.stderr,
        )
        return 1

    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    bridge = os.path.join(repo, "bridge", "bridge.py")
    if not os.path.isfile(bridge):
        # fall back to MERCURY_REPO-relative
        repo2 = os.environ.get("MERCURY_REPO", "").strip()
        cand = os.path.join(repo2, "bridge", "bridge.py") if repo2 else ""
        if cand and os.path.isfile(cand):
            bridge = cand
        else:
            print(f"omp: bridge not found at {bridge}", file=sys.stderr)
            return 1

    # Render omp policy + model config (fail-hard on bad slots).
    # MERCURY-OMP PATCH (C2): --render-omp refreshes privilege inheritance
    # (mercury approvals.deny → omp bash.patterns deny) so this spawn sees
    # the CURRENT deny rules, not a stale omp: subtree.
    rr = subprocess.run(
        [sys.executable, bridge, "--render-omp"],
        capture_output=True, text=True, timeout=60,
    )
    if rr.returncode != 0:
        print(rr.stderr.strip(), file=sys.stderr)
        return 1

    # Render the delegate model via the bridge (fail-hard on bad slots).
    br = subprocess.run(
        [sys.executable, bridge, "--delegate"],
        capture_output=True, text=True, timeout=60,
    )
    if br.returncode != 0:
        print(br.stderr.strip(), file=sys.stderr)
        return 1
    model = ""
    env_overrides = {}
    for line in br.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env_overrides.setdefault(k, v)
            if k == "OMP_MODEL":
                model = v

    env = dict(os.environ)
    env.setdefault("MERCURY_CONFIG", os.path.expanduser("~/.mercury/config.yaml"))
    env.setdefault("MERCURY_HOME", os.path.expanduser("~/.mercury"))
    # HERMES-OMP PATCH (Nous search inheritance): when hermes' web selection
    # is the Nous-managed gateway, bridge it into omp's NATIVE firecrawl env
    # so `mercury omp` search/scrape rides the same gateway + credentials.
    try:
        from tools.omp_delegation import _nous_search_env_overrides
        for _k, _v in _nous_search_env_overrides().items():
            env.setdefault(_k, _v)
    except Exception:
        pass
    for k, v in env_overrides.items():
        env.setdefault(k, v)

    passthrough = list(getattr(args, "omp_args", None) or [])
    # No explicit model in the passthrough → pin the delegate model.
    if not any(a == "--model" or a.startswith("--model=") for a in passthrough):
        passthrough = ["--model", model] + passthrough

    if getattr(args, "print_cmd", False):
        print(shlex.join([omp_bin] + passthrough))
        return 0

    os.execve(omp_bin, [omp_bin] + passthrough, env)  # never returns
    return 0
