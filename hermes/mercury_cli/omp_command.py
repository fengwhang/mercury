"""MERCURY-OMP PATCH (NB-2): `omp` subcommand — launch the omp half of Mercury.

`mercury omp [args…]` / `mercury omp [args…]` execs the patched omp build as
its own somewhat-independent program: it feels like original omp (full TUI,
all fan-out features) but with model roles structurally stripped. The model
comes from the selected profile's delegate_model slot via the bridge.
An initial -p/--profile selects the Mercury profile; native argv follows
unchanged. Use -- before native -p to request OMP's short print option.
"""
import os
import subprocess
import shlex
import sys
from pathlib import Path


def omp_profile_env(profile_home=None, base_env=None) -> dict[str, str]:
    """Build a child's environment without changing the gateway's environment."""
    from mercury_constants import get_hermes_home, named_profile_home
    from mercury_cli.profiles import ensure_profile_prompt_files

    env = dict(os.environ if base_env is None else base_env)
    home = Path(profile_home) if profile_home is not None else get_hermes_home()
    profile = named_profile_home(home)
    if profile is not None:
        from mercury_constants import assert_named_profile_home_live
        assert_named_profile_home_live(profile)
        ensure_profile_prompt_files(profile)
        env.update({
            "HERMES_HOME": str(profile),
            "MERCURY_PROFILE_HOME": str(profile),
            "MERCURY_SKILLS_DIR": str(profile / "skills"),
            "MERCURY_CONFIG": str(profile / "config.yaml"),
            "HERMES_OMP_CONFIG": str(profile / "config.yaml"),
            "PI_CODING_AGENT_DIR": str(profile / "omp" / "agent"),
        })
    else:
        env.pop("MERCURY_PROFILE_HOME", None)
    return env


def split_omp_profile_args(argv: list[str]) -> tuple[str | None, list[str]]:
    """Reserve profile selectors before native argv; `--` protects native -p."""
    profile = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--print-cmd":
            index += 1
            continue
        if arg in ("-p", "--profile"):
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise ValueError("omp: -p/--profile requires a profile name")
            value, size = argv[index + 1], 2
        elif arg.startswith("--profile="):
            value, size = arg.partition("=")[2], 1
        else:
            break
        if profile is not None:
            raise ValueError("omp: choose one profile")
        profile = value
        argv = argv[:index] + argv[index + size:]
    return profile, argv


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
    try:
        env = omp_profile_env()
    except (OSError, ValueError) as exc:
        print(f"omp: {exc}", file=sys.stderr)
        return 1
    env.setdefault("MERCURY_HOME", os.path.expanduser("~/.mercury"))
    env.setdefault("MERCURY_CONFIG", str(Path(env["MERCURY_HOME"]) / "config.yaml"))
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
        capture_output=True, text=True, timeout=60, env=env,
    )
    if rr.returncode != 0:
        print(rr.stderr.strip(), file=sys.stderr)
        return 1

    # Render the delegate model via the bridge (fail-hard on bad slots).
    br = subprocess.run(
        [sys.executable, bridge, "--delegate"],
        capture_output=True, text=True, timeout=60, env=env,
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
        if env.get("MERCURY_PROFILE_HOME"):
            env[k] = v
        else:
            env.setdefault(k, v)

    passthrough = list(getattr(args, "omp_args", None) or [])
    if passthrough[:1] == ["--"]:
        passthrough = passthrough[1:]
    # No explicit model in the passthrough → pin the delegate model.
    if not any(a == "--model" or a.startswith("--model=") for a in passthrough):
        passthrough = ["--model", model] + passthrough
        # The native defaultThinkingLevel setting has no "off" value.
        # Pass the configured effort explicitly, including a selected disable,
        # while preserving a user's explicit model or thinking override.
        if not any(a == "--thinking" or a.startswith("--thinking=") for a in passthrough):
            thinking = env_overrides.get("OMP_THINKING_LEVEL")
            if thinking:
                passthrough = ["--thinking", thinking] + passthrough

    if getattr(args, "print_cmd", False):
        print(shlex.join([omp_bin] + passthrough))
        return 0

    os.execve(omp_bin, [omp_bin] + passthrough, env)  # never returns
    return 0


def cmd_stats(args) -> int:
    """`mercury stats` — dual-engine usage dashboard (Hermes + OMP).

    Launches the vendored ``omp stats`` webserver with the dual-engine metrics
    API enabled, so the dashboard reports both the Hermes personal-agent engine
    and the OMP coding engine. The stock ``mercury omp stats`` path stays the
    untouched single-engine passthrough; only this command opts into ``--engines``.
    """
    try:
        env = omp_profile_env()
    except (OSError, ValueError) as exc:
        print(f"stats: {exc}", file=sys.stderr)
        return 1
    env.setdefault("MERCURY_HOME", os.path.expanduser("~/.mercury"))
    env.setdefault("MERCURY_CONFIG", str(Path(env["MERCURY_HOME"]) / "config.yaml"))
    omp_bin = _resolve_omp_binary()
    if not omp_bin:
        print(
            "stats: no omp binary found. Set HERMES_OMP_BIN or build the vendored tree\n"
            "(cd omp && bun install && bun run build:bindings && bun run build in\n"
            "packages/coding-agent).",
            file=sys.stderr,
        )
        return 1

    passthrough = ["stats", "--engines"]
    port = getattr(args, "port", None)
    if port is not None:
        passthrough += ["--port", str(port)]
    host = getattr(args, "host", None)
    if host:
        passthrough += ["--host", str(host)]
    if getattr(args, "json", False):
        passthrough.append("--json")
    if getattr(args, "summary", False):
        passthrough.append("--summary")

    os.execve(omp_bin, [omp_bin] + passthrough, env)  # never returns
    return 0
