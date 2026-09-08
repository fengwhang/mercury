"""Tuwunel binary lifecycle: query, gate, download, install, refresh.

Spec D16: fetched at install time from the LATEST STABLE upstream release
(github.com/matrix-construct/tuwunel) — NOT vendored — pinned to a hard
minimum of 1.8.1 (Synapse admin API introduction). Assets are single static
ELF binaries, zstd-compressed, named::

    v{tag}-release-all-{arch}-linux-gnu-tuwunel.zst
    arch = x86_64-v{1,2,3} | aarch64-v8

We always select the LOWEST x86_64 microarch (v1) — universally compatible,
no AVX requirement roulette. The GitHub query boundary is injectable
(``fetch=``) so install dry-runs and unit tests never touch the network.
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from observatory.config_gen import ObservatoryPaths

TUWUNEL_REPO = "matrix-construct/tuwunel"
RELEASES_LATEST_API = f"https://api.github.com/repos/{TUWUNEL_REPO}/releases/latest"

#: Hard minimum (spec §2 component 1): 1.8.1 introduced the Synapse admin API.
MIN_VERSION = "1.8.1"

_USER_AGENT = "mercury-installer"

Fetch = Callable[[str], bytes]


class TuwunelError(RuntimeError):
    """Fail-hard installer error: clear message, no partial state kept."""


# --- versions -----------------------------------------------------------------

def parse_version(tag: str) -> tuple[int, ...]:
    """'v1.9.0' -> (1, 9, 0). Raises on unparseable input."""
    nums = re.findall(r"\d+", tag or "")
    if not nums:
        raise TuwunelError(f"cannot parse a version from release tag {tag!r}")
    return tuple(int(n) for n in nums[:3])


def check_min_version(tag: str) -> tuple[int, ...]:
    """Enforce the >= MIN_VERSION gate. Returns the parsed version."""
    got = parse_version(tag)
    if got < parse_version(MIN_VERSION):
        raise TuwunelError(
            f"latest stable Tuwunel release is v{'.'.join(map(str, got))} "
            f"but Mercury requires >= {MIN_VERSION} (Synapse admin API). "
            f"Refusing to install; see {RELEASES_LATEST_API.replace('/releases/latest', '/releases')}"
        )
    return got


# --- host arch ----------------------------------------------------------------

def host_asset_arch(machine: str | None = None) -> str:
    """Release-asset arch token for this host (x86_64-v1 baseline by design)."""
    m = (machine or platform.machine()).lower()
    if m in ("x86_64", "amd64"):
        return "x86_64-v1"
    if m in ("aarch64", "arm64"):
        return "aarch64-v8"
    raise TuwunelError(
        f"unsupported CPU architecture for Tuwunel: {m or 'unknown'} "
        "(supported: x86_64/amd64, aarch64/arm64)"
    )


def _verify_elf_arch(path: Path, asset_arch: str) -> None:
    """ELF e_machine LSB at offset 18 (same law as the omp check in
    install.sh): 62 = EM_X86_64, 183 = EM_AARCH64. A wrong-arch binary must
    never be installed 'successfully'."""
    want = 62 if asset_arch.startswith("x86_64") else 183
    with open(path, "rb") as f:
        head = f.read(20)
    if len(head) < 20 or head[:4] != b"\x7fELF":
        raise TuwunelError(f"downloaded asset is not an ELF binary: {path}")
    got = head[18]
    if got != want:
        raise TuwunelError(
            f"WRONG ARCH: downloaded Tuwunel is ELF machine {got} "
            f"(wanted {want} for {asset_arch}) — refusing to install"
        )


# --- GitHub boundary ------------------------------------------------------------

def _default_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise TuwunelError(f"fetch failed: {url} ({exc})") from exc


def latest_stable_release(fetch: Fetch | None = None) -> dict:
    """Latest STABLE release dict from the GitHub API (``/releases/latest``
    excludes prereleases/drafts by definition) with the >= MIN_VERSION gate
    enforced. Raises TuwunelError on any failure — the installer's fail-hard
    law."""
    fetch = fetch or _default_fetch
    try:
        payload = fetch(RELEASES_LATEST_API)
        release = json.loads(payload.decode("utf-8"))
    except TuwunelError:
        raise
    except Exception as exc:
        raise TuwunelError(
            f"could not query the Tuwunel release API ({exc}). "
            "Check connectivity, then retry the install."
        ) from exc
    tag = str(release.get("tag_name") or "")
    if not tag:
        raise TuwunelError("Tuwunel release payload has no tag_name — aborting")
    check_min_version(tag)
    return release


def pick_asset(release: dict, asset_arch: str) -> tuple[str, str]:
    """(asset_name, download_url) for the release's static-binary asset.

    Exact ``-release-all-{arch}-`` match first; otherwise the lowest
    microarch variant of the same family (x86_64-v1 < -v2 < -v3).
    """
    assets = {str(a.get("name", "")): a for a in release.get("assets", [])}
    tag = str(release.get("tag_name") or "")
    exact = f"{tag}-release-all-{asset_arch}-linux-gnu-tuwunel.zst"
    if exact in assets and assets[exact].get("browser_download_url"):
        return exact, str(assets[exact]["browser_download_url"])
    family = asset_arch.split("-")[0]  # x86_64 | aarch64
    pat = re.compile(
        rf"^{re.escape(tag)}-release-all-{re.escape(family)}-v\d+-linux-gnu-tuwunel\.zst$"
    )
    candidates = sorted(n for n in assets if pat.match(n))
    if not candidates:
        raise TuwunelError(
            f"release {tag or '?'} has no static Tuwunel binary for {asset_arch} "
            f"(looked for {exact}); available: {', '.join(assets) or 'none'}"
        )
    name = candidates[0]
    url = assets[name].get("browser_download_url")
    if not url:
        raise TuwunelError(f"asset {name} has no browser_download_url")
    return name, str(url)


# --- install / refresh -----------------------------------------------------------

def decompress_zst(src: Path, dest: Path) -> None:
    """Decompress the single-binary .zst asset via the zstd CLI."""
    try:
        with open(dest, "wb") as out:
            subprocess.run(
                ["zstd", "-d", "-c", str(src)],
                stdout=out,
                stderr=subprocess.PIPE,
                check=True,
                timeout=300,
            )
    except FileNotFoundError as exc:
        raise TuwunelError(
            "the 'zstd' decompressor is not installed — Tuwunel ships as a "
            "zstd-compressed binary (install zstd: apt/dnf install zstd)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise TuwunelError(
            f"zstd failed on {src.name}: {(exc.stderr or b'').decode(errors='replace').strip()}"
        ) from exc


def install_tuwunel(paths: ObservatoryPaths, *, fetch: Fetch | None = None,
                    release: dict | None = None) -> str:
    """Install the latest stable Tuwunel binary (atomic replace) + version file.

    Returns the installed version (no leading 'v'). Hard-fails on: API
    unreachable, version below the gate, missing asset, wrong-arch ELF,
    decompress error. ``release`` lets a caller that already queried the
    API (refresh_tuwunel) pass its result through and skip the second query.
    """
    fetch = fetch or _default_fetch
    release = release or latest_stable_release(fetch)
    tag = str(release["tag_name"])
    version = tag.lstrip("v")
    asset_arch = host_asset_arch()
    _name, url = pick_asset(release, asset_arch)

    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    zst_tmp = paths.bin_dir / ".tuwunel.download.zst"
    bin_tmp = paths.bin_dir / ".tuwunel.download.bin"
    try:
        zst_tmp.write_bytes(fetch(url))
        decompress_zst(zst_tmp, bin_tmp)
        _verify_elf_arch(bin_tmp, asset_arch)
        bin_tmp.chmod(0o755)
        # atomic: the live binary is never absent or half-written
        bin_tmp.replace(paths.binary)
        paths.version_file.write_text(version + "\n", encoding="utf-8")
    finally:
        zst_tmp.unlink(missing_ok=True)
        bin_tmp.unlink(missing_ok=True)
    return version


def installed_version(paths: ObservatoryPaths) -> str | None:
    """Recorded version, only when BOTH the binary and the version file exist."""
    if not (paths.binary.is_file() and paths.binary.stat().st_size > 0
            and paths.version_file.is_file()):
        return None
    ver = paths.version_file.read_text(encoding="utf-8").strip()
    return ver or None


def refresh_tuwunel(paths: ObservatoryPaths, *, fetch: Fetch | None = None,
                    force: bool = False) -> tuple[str, str]:
    """Bring the installed binary to latest stable. Returns (action, version):

    ('current', v) — already at/above latest, nothing downloaded;
    ('installed', v) — no prior binary (first install / adoption);
    ('updated', v) — replaced an older binary.
    """
    release = latest_stable_release(fetch)
    latest = str(release["tag_name"]).lstrip("v")
    current = installed_version(paths)
    if current and not force and parse_version(current) >= parse_version(latest):
        return "current", current
    version = install_tuwunel(paths, fetch=fetch, release=release)
    return ("installed" if current is None else "updated"), version
