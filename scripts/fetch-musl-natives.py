#!/usr/bin/env python3
"""Stage version-matched musl addons from upstream's compiled release.

The upstream npm leaf packages only carry glibc Linux addons. Fetch the exact
native package version (never latest), verify GitHub's SHA-256 digest, and read
its embedded addon archive without executing the downloaded binary.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes"))
from mercury_cli.elf import check_elf, read_elf  # noqa: E402


def extract_addons(binary: Path, arch: str, version: str, destination: Path) -> None:
    data = binary.read_bytes()
    allowed = ({f"pi_natives.linux-{arch}-{v}.node" for v in ("baseline", "modern")}
               if arch == "x64" else {f"pi_natives.linux-{arch}.node"})
    sentinel = f"__piNativesV{version.replace('.', '_')}".encode()
    offset = 0
    while (offset := data.find(b"\x1f\x8b\x08", offset)) != -1:
        try:
            decoder = zlib.decompressobj(31)
            payload = decoder.decompress(data[offset:], 512 * 1024 * 1024)
            if not decoder.eof:
                raise ValueError("Oversized or incomplete embedded archive")
            with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                members = archive.getmembers()
                if not members or not {m.name for m in members} <= allowed:
                    raise ValueError("Not the native addon archive")
                staged = {}
                for member in members:
                    stream = archive.extractfile(member) if member.isfile() else None
                    if stream is None:
                        raise ValueError("Native addon must be a regular file")
                    content = stream.read()
                    check_elf(read_elf(io.BytesIO(content)), arch, "musl", portable=True, addon=True)
                    if sentinel not in content:
                        raise ValueError(f"Native version mismatch: {member.name}")
                    staged[member.name] = content
                destination.mkdir(parents=True, exist_ok=True)
                for name, content in staged.items():
                    (destination / name).write_bytes(content)
                    print(f"Staged {name} ({version}, musl)", flush=True)
                return
        except (zlib.error, tarfile.TarError, ValueError):
            offset += 3
    raise ValueError(f"No valid {arch}/musl native archive for {version}")


def main() -> None:
    native = ROOT / "omp/packages/natives"
    version = json.loads((native / "package.json").read_text())["version"]
    url = f"https://api.github.com/repos/can1357/oh-my-pi/releases/tags/v{version}"
    with urllib.request.urlopen(url, timeout=60) as response:
        release = json.load(response)
    for arch in ("x64", "arm64"):
        name = f"omp-linux-musl-{arch}"
        asset = next(a for a in release["assets"] if a["name"] == name)
        digest = asset.get("digest", "")
        if not digest.startswith("sha256:"):
            raise ValueError(f"Missing trusted SHA-256 digest for {name}")
        with tempfile.TemporaryDirectory(prefix="mercury-musl-") as temporary:
            binary = Path(temporary) / name
            with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response, binary.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
            with binary.open("rb") as stream:
                actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                raise ValueError(f"Checksum mismatch for {name}")
            extract_addons(binary, arch, version, native / "native/musl")


if __name__ == "__main__":
    main()
