"""MERCURY-OMP PATCH: tarball-release updater for `mercury update`.

Mercury installs from a distribution tarball (no .git), so upstream's
git-based update path cannot work. This module implements the release
flow against the Mercury repository:

  1. query the GitHub API for the latest published release
  2. compare with the installed __version__ — exit clean when current
  3. download the tarball + its .sha256, verify, unpack to a temp dir
  4. swap the code tree in place (preserving .venv, .git, .env, dist
     artifacts) — same preservation set as upstream's ZIP path
  5. re-run `uv pip install -e .` so entry points stay consistent
  6. remind that ~/.mercury state is untouched

Git checkouts (dev trees) keep using upstream's git logic with the
official remote rewritten to fengwhang/mercury.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MERCURY_REPO_OWNER = "fengwhang"
MERCURY_REPO_NAME = "mercury"
RELEASES_API = f"https://api.github.com/repos/{MERCURY_REPO_OWNER}/{MERCURY_REPO_NAME}/releases/latest"

PRESERVED_TOP_LEVEL = {".venv", "venv", ".git", ".env", "dist", "node_modules", ".mercury", ".mercury-build-id"}


def _project_root() -> Path:
    """The Mercury INSTALL ROOT (the dir containing bin/, hermes/, omp/).

    MERCURY-OMP PATCH (bug #5 follow-up, THE root cause): upstream's
    fallback `Path(__file__).resolve().parents[1]` assumes the repo layout
    (mercury_cli at <root>/hermes/mercury_cli). In the Mercury TARBALL
    layout that resolves to <INSTALL>/hermes — one level too deep. The
    updater then swapped the whole new tree INTO hermes/, creating
    hermes/bin, hermes/omp, hermes/hermes junk, never touching the real
    <INSTALL>/omp or <INSTALL>/hermes/tools/*, and printed success while
    every byte the engine runs stayed stale. Detect the install root by
    structure: walk up until a dir contains BOTH bin/mercury and hermes/;
    require it, never guess silently.
    """
    here = Path(__file__).resolve()
    for cand in (here.parents[1], here.parents[2], here.parents[3]):
        if (cand / "bin" / "mercury").exists() and (cand / "hermes").is_dir():
            return cand
    # Last-resort sanity check: if the fallback would point at a dir that
    # ALREADY contains a nested 'mercury' layout (hermes/omp inside hermes/),
    # refuse loudly rather than corrupting the tree again.
    fallback = here.parents[1]
    if (fallback / "bin" / "mercury").exists() and (fallback / "hermes").is_dir():
        return fallback
    print("✗ Cannot locate the Mercury install root "
          f"(looked above {here}; no parent has bin/mercury + hermes/).")
    print("  Refusing to swap files into a guessed location. Aborting.")
    raise SystemExit(1)


def _installed_version() -> str:
    try:
        from mercury_cli import __version__
        return str(__version__)
    except Exception:
        return "0"


def _installed_build_id(root: Path | None = None) -> str:
    """Sha256 of the tarball this tree was installed/updated from.

    Empty when unknown (fresh pre-0.0.4 installs). MERCURY-OMP PATCH
    (bug #5 follow-up): version tags alone proved insufficient — same-tag
    asset re-updates made `mercury update` a silent no-op while bytes
    drifted. The build id is the content truth.
    """
    f = (root or _project_root()) / ".mercury-build-id"
    try:
        return f.read_text().strip()
    except Exception:
        return ""


def _record_build_id(root: Path, build_id: str) -> None:
    try:
        (root / ".mercury-build-id").write_text(build_id + "\n")
    except Exception:
        pass


def _release_sha256(assets: dict, tar_name: str) -> str | None:
    """Fetch the release's .sha256 sidecar (tiny text) — None if absent."""
    sha_asset = assets.get(f"{tar_name}.sha256")
    if not sha_asset or not sha_asset.get("browser_download_url"):
        return None
    try:
        import urllib.request

        req = urllib.request.Request(
            sha_asset["browser_download_url"], headers={"User-Agent": "mercury-update"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode().split()[0]
    except Exception:
        return None


def _normalize(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _latest_release(timeout: float = 20.0) -> dict | None:
    req = urllib.request.Request(RELEASES_API, headers={"User-Agent": "mercury-update"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def is_current() -> bool:
    rel = _latest_release()
    if not rel:
        return True  # cannot check -> don't nag
    latest = str(rel.get("tag_name", "")).lstrip("v")
    return _normalize(latest) <= _normalize(_installed_version())


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "mercury-update"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _swap_tree_preserving(src: Path, dst: Path, keep: set[str]) -> None:
    """Recursively replace src into dst but never touch entries in `keep`."""
    keep_paths = {dst / name for name in keep}
    for entry in src.iterdir():
        target = dst / entry.name
        if target in keep_paths:
            continue
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)


def _swap_tree(src: Path, dst: Path) -> None:
    """Replace dst's contents with src's, preserving dst's protected entries.

    MERCURY-OMP PATCH: the venv lives at hermes/.venv (inside the hermes
    engine dir), so a whole-tree rsync-style replace would delete it. The
    tarball has no hermes/.venv; preserve dst's when the tarball lacks it.
    """
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.name in PRESERVED_TOP_LEVEL:
            continue
        # nested preserve: hermes/.venv survives even though "hermes" is
        # replaced wholesale (copytree would rmtree it first).
        if entry.name == "hermes" and (dst / "hermes" / ".venv").exists():
            _swap_tree_preserving(entry, target, {".venv"})
            continue
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)


def update_from_release(*, assume_yes: bool = False) -> int:
    """Run the release update. Returns a process exit code."""
    print("🌡️ Updating Mercury (release channel: "
          f"{MERCURY_REPO_OWNER}/{MERCURY_REPO_NAME})...")
    print()

    rel = _latest_release()
    if not rel:
        print("✗ Could not reach the GitHub API for release info.")
        print("  Check connectivity, or update manually from:")
        print(f"  https://github.com/{MERCURY_REPO_OWNER}/{MERCURY_REPO_NAME}/releases")
        return 1

    latest = str(rel.get("tag_name", "")).lstrip("v")
    current = _installed_version()
    if _normalize(latest) <= _normalize(current):
        # MERCURY-OMP PATCH (bug #5 follow-up): tag equality is NOT proof of
        # content equality. Compare the release tarball's sha256 (tiny text
        # fetch) against the recorded build id; a mismatch (or unknown id on
        # a fresh tree) forces the update even at the same version.
        _assets_probe = {a.get("name", ""): a for a in rel.get("assets", [])}
        _m = __import__("platform").machine().lower()
        _arch = "arm64" if _m in ("aarch64", "arm64") else "x64" if _m in ("x86_64", "amd64") else ""
        _probe_names = []
        if _arch:
            _probe_names += [f"mercury-{latest}-{_arch}.tar.gz", f"mercury-{_arch}.tar.gz"]
        _probe_names += [f"mercury-{latest}.tar.gz"]
        _probe_tar = next((n for n in _probe_names if n in _assets_probe), "")
        _rel_sha = _release_sha256(_assets_probe, _probe_tar) if _probe_tar else None
        _inst_sha = _installed_build_id()
        if _rel_sha is None or (_inst_sha and _rel_sha == _inst_sha):
            print(f"✓ Mercury is up to date (v{current}; latest release v{latest}).")
            return 0
        print(f"→ v{current} (build {_inst_sha[:12] or 'unknown'}) -> v{latest} "
              f"(build {_rel_sha[:12]}) — same tag, different bytes; updating.")

    print(f"→ v{current} -> v{latest}")

    assets = {a.get("name", ""): a for a in rel.get("assets", [])}
    # MERCURY-OMP PATCH: releases publish PER-ARCH tarballs — select the one
    # for THIS host (uname -m -> x64/arm64), with the version-less alias
    # and single-candidate fallbacks for older release layouts.
    _m = __import__("platform").machine().lower()
    _arch = "arm64" if _m in ("aarch64", "arm64") else "x64" if _m in ("x86_64", "amd64") else ""
    candidates = []
    if _arch:
        candidates += [f"mercury-{latest}-{_arch}.tar.gz", f"mercury-{_arch}.tar.gz"]
    candidates += [f"mercury-{latest}.tar.gz"]
    tar_name = next((n for n in candidates if n in assets), "")
    asset = assets.get(tar_name) if tar_name else None
    if not asset:
        cands = [a for n, a in assets.items() if re.match(r"mercury-.*\.tar\.gz$", n)]
        if len(cands) == 1:
            asset = cands[0]
            tar_name = next(n for n, a in assets.items() if a is asset)
    if not asset:
        print(f"✗ Release v{latest} has no tarball for this host ({_m or 'unknown arch'}).")
        print(f"  Looked for: {', '.join(candidates)}")
        return 1
    url = asset.get("browser_download_url")
    if not url:
        print("✗ Release asset has no download URL.")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="mercury-update-"))
    try:
        print("→ Downloading tarball...")
        tar_path = tmp / tar_name
        _download(url, tar_path)

        # MERCURY-OMP PATCH (arch guard — same law as install.sh): verify the
        # packed omp binary's ELF machine byte matches the host BEFORE
        # unpacking over the live tree. Byte 18: 62=x86_64, 183=AArch64.
        try:
            import tarfile as _tf

            with _tf.open(tar_path, "r:gz") as _tar:
                _member = next(
                    (m for m in _tar.getnames()
                     if m.endswith("omp/packages/coding-agent/dist/omp")),
                    None,
                )
                if _member is not None:
                    _f = _tar.extractfile(_member)
                    if _f is not None:
                        _f.read(18)
                        _magic = _f.read(1)[0]
                        _want = 183 if _arch == "arm64" else 62 if _arch == "x64" else None
                        if _want is not None and _magic != _want:
                            print(f"✗ WRONG ARCH: tarball omp is {'AArch64' if _magic == 183 else f'ELF {_magic}'}"
                                  f" but this host is {_m} — refusing to install an emulated binary.")
                            return 1
        except Exception as _exc:
            print(f"⚠ arch pre-check skipped ({_exc}) — checksum still enforced")

        sha_asset = assets.get(f"{tar_name}.sha256")
        if sha_asset and sha_asset.get("browser_download_url"):
            sha_path = tmp / f"{tar_name}.sha256"
            try:
                _download(sha_asset["browser_download_url"], sha_path)
                want = sha_path.read_text().split()[0]
                have = _sha256(tar_path)
                if want != have:
                    print("✗ Checksum mismatch — corrupt download. Aborting.")
                    return 1
                print("  checksum verified")
            except Exception as exc:
                print(f"  ⚠ checksum step failed ({exc}) — continuing without it")

        print("→ Unpacking...")
        import tarfile
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(tmp)  # noqa: S202 — GitHub-sourced artifact, path members verified below
        src = tmp / "mercury"
        if not (src / "bin" / "mercury").exists():
            print("✗ Tarball layout unexpected (bin/mercury missing). Aborting.")
            return 1

        root = _project_root()
        print(f"→ Swapping code tree at {root} (state in ~/.mercury preserved)...")
        _swap_tree(src, root)

        # MERCURY-OMP PATCH (bug #5 follow-up): PROVE the swap landed. The
        # tarball's own omp binary must be at the install root's omp/ path,
        # byte-identical to what we unpacked. A wrong root (the historic
        # <INSTALL>/hermes mis-swap) fails HERE instead of reporting success.
        _tar_omp_rel = "omp/packages/coding-agent/dist/omp"
        _want = src / _tar_omp_rel
        _got = root / _tar_omp_rel
        if _want.exists():
            if not _got.exists() or _sha256(_want) != _sha256(_got):
                print("✗ Update verification FAILED: swapped omp binary does not "
                      f"match the tarball at {_got}. The swap wrote the wrong "
                      "location — no files were (correctly) updated.")
                return 1
            print("  swap verified: omp binary matches tarball bytes")
        # and the delegation module the engine imports
        _tar_del = src / "hermes" / "tools" / "omp_delegation.py"
        _got_del = root / "hermes" / "tools" / "omp_delegation.py"
        if _tar_del.exists() and (
                not _got_del.exists() or _sha256(_tar_del) != _sha256(_got_del)):
            print("✗ Update verification FAILED: hermes/tools/omp_delegation.py "
                  "does not match the tarball after the swap.")
            return 1
        print("  swap verified: omp_delegation.py matches tarball bytes")

        # refresh the editable install so entry points/scripts stay aligned
        venv = root / "hermes" / ".venv"
        if venv.exists():
            try:
                print("→ Refreshing python environment...")
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "-e",
                     str(root / "hermes")],
                    check=True, capture_output=True,
                )
            except Exception as exc:
                print(f"  ⚠ pip refresh failed ({exc}) — run: cd {root}/hermes && uv pip install -e .")

        _new_sha = _sha256(tar_path)
        _record_build_id(root, _new_sha)
        print()
        print(f"✓ Mercury updated to v{latest} (build {_new_sha[:12]}).")
        print("  Restart any running sessions to pick up the new code.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
