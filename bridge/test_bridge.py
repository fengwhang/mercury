#!/usr/bin/env python3
"""Tests for bridge v3 — unified ~/.mercury/config.yaml semantics."""
import os
import subprocess
import sys
import tempfile

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
PY = sys.executable


def run(args, cfg):
    return subprocess.run([PY, BRIDGE] + args, capture_output=True, text=True,
                          env={**os.environ, "HERMES_OMP_CONFIG": cfg})


def write_cfg(tmp, text):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, dir=tmp)
    f.write(text)
    f.close()
    return f.name


def base(**kw):
    m = {"default": "prov/m-1", "fallback": "prov/m-2",
         "delegate_model": "prov/m-1", "delegate_fallback": "prov/m-2"}
    m.update(kw)
    return "models:\n" + "\n".join(f"  {k}: {v}" for k, v in m.items() if v) + "\n"


def main():
    tmp = tempfile.mkdtemp()
    failures = []

    def expect(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    cfg = write_cfg(tmp, base())
    r = run([], cfg)
    expect("full config: exit 0", r.returncode == 0, r.stderr)
    expect("renders DEFAULT slot (new name)", "DEFAULT=prov/m-1" in r.stdout, r.stdout)
    expect("renders DELEGATE_MODEL slot (new name)", "DELEGATE_MODEL=prov/m-1" in r.stdout, r.stdout)

    r = run(["--delegate"], cfg)
    expect("delegate: OMP_MODEL from delegate_model", r.returncode == 0 and "OMP_MODEL=prov/m-1" in r.stdout, r.stderr)

    cfg_nf = write_cfg(tmp, base(fallback=""))
    r = run([], cfg_nf)
    expect("missing fallback: exit 1", r.returncode == 1 and "fallback" in r.stderr)

    cfg_df = write_cfg(tmp, base(delegate_fallback=""))
    r = run(["--delegate"], cfg_df)
    expect("missing delegate_fallback: exit 1 under --delegate", r.returncode == 1)
    r = run([], cfg_df)
    expect("missing delegate_fallback: OK w/o --delegate", r.returncode == 0 and "DELEGATE_FALLBACK=<unset>" in r.stdout)

    cfg_same = write_cfg(tmp, base(fallback="prov/m-1"))
    r = run([], cfg_same)
    expect("default==fallback: exit 1", r.returncode == 1)

    cfg_dsame = write_cfg(tmp, base(delegate_fallback="prov/m-1"))
    r = run(["--delegate"], cfg_dsame)
    expect("delegate_model==delegate_fallback: exit 1", r.returncode == 1)

    r = run([], os.path.join(tmp, "nonexistent.yaml"))
    expect("no config file: exit 1", r.returncode == 1)

    # --render-omp writes the omp: subtree, PRESERVING other subtrees
    unified = write_cfg(tmp, base() + "\nhermes:\n  some_key: keep-me\n")
    r = run(["--render-omp"], unified)
    expect("render-omp: exit 0", r.returncode == 0, r.stderr)
    out = open(unified).read()
    expect("render-omp: omp: subtree written",
           "omp:" in out and "fallbackChains" in out and "fallbackChain:" not in out)
    expect("render-omp: hermes: subtree preserved", "keep-me" in out)
    expect("render-omp: models: preserved", "delegate_model: prov/m-1" in out)
    expect("render-omp: NO modelRoles written", "modelRoles" not in out)

    # idempotent re-render
    r = run(["--render-omp"], unified)
    out2 = open(unified).read()
    expect("render-omp: idempotent", r.returncode == 0 and out2.count("omp:") == 1 and "keep-me" in out2)

    # --- C2: privilege inheritance (approvals.deny → bash.patterns) ---

    def deny_cfg(**extra_hermes):
        h = "hermes:\n  approvals:\n    deny:\n      - 'rm *'\n      - 'shutdown*'\n"
        if extra_hermes:
            h += "".join(f"  {k}: {v}\n" for k, v in extra_hermes.items())
        return write_cfg(tmp, base() + "\n" + h)

    # basic translation
    c = deny_cfg()
    r = run(["--render-omp"], c)
    expect("C2: exit 0", r.returncode == 0, r.stderr)
    out = open(c).read()
    expect("C2: bash.patterns deny written",
           "patterns:" in out and "approval: deny" in out and "match: 'rm *'" in out, out)
    expect("C2: hermes: subtree still preserved", "approvals:" in out and "deny:" in out)

    # idempotent: re-render doesn't duplicate deny rules
    r = run(["--render-omp"], c)
    out = open(c).read()
    expect("C2: idempotent deny rules", r.returncode == 0 and out.count("approval: deny") == 2, out)

    # no deny list → no bash.patterns written (unchanged render)
    c_none = write_cfg(tmp, base() + "\nhermes:\n  approvals:\n    mode: ask\n")
    r = run(["--render-omp"], c_none)
    out = open(c_none).read()
    expect("C2: no deny → no bash.patterns", r.returncode == 0 and "patterns:" not in out, out)

    # sibling deny: NOT under approvals: → ignored
    c_sib = write_cfg(tmp, base() + "\nhermes:\n  tools:\n    deny:\n      - 'kill *'\n  approvals:\n    deny:\n      - 'rm *'\n")
    r = run(["--render-omp"], c_sib)
    out = open(c_sib).read()
    expect("C2: sibling deny ignored",
           "match: 'kill *'" not in out and "match: 'rm *'" in out, out)

    # fnmatch widening: ? and [seq] → *
    c_wide = write_cfg(tmp, base() + "\nhermes:\n  approvals:\n    deny:\n      - 'mkfs?[abc]'\n      - 'dd if=*of=/dev/sd?'\n      - \"it's\"\n")
    r = run(["--render-omp"], c_wide)
    out = open(c_wide).read()
    expect("C2: fnmatch widened to *",
           "match: 'mkfs*'" in out and "match: 'dd if=*of=/dev/sd*'" in out, out)
    expect("C2: YAML single-quote escaping", "match: 'it''s'" in out, out)

    # case: hermes matches case-insensitively; omp keeps verbatim (documented delta)
    c_case = write_cfg(tmp, base() + "\nhermes:\n  approvals:\n    deny:\n      - 'systemctl restart*'\n")
    r = run(["--render-omp"], c_case)
    out = open(c_case).read()
    expect("C2: pattern carried verbatim", "match: 'systemctl restart*'" in out, out)

    r = run(["--check"], cfg)
    expect("--check: silent exit 0", r.returncode == 0 and r.stdout == "")

    print()
    if failures:
        print(f"{len(failures)} FAILED"); sys.exit(1)
    print("all pass")


if __name__ == "__main__":
    main()
