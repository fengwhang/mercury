"""Read Linux binary compatibility without executing it or requiring binutils.

Shared by the release packer, installer and updater. Only the supported 64-bit
Linux architectures are accepted. Native addons may omit PT_INTERP.
"""
from __future__ import annotations

import argparse
import os
import platform
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MACHINES = {"x64": 62, "arm64": 183}
LOADERS = {
    ("x64", "glibc"): "/lib64/ld-linux-x86-64.so.2",
    ("arm64", "glibc"): "/lib/ld-linux-aarch64.so.1",
    ("x64", "musl"): "/lib/ld-musl-x86_64.so.1",
    ("arm64", "musl"): "/lib/ld-musl-aarch64.so.1",
}


@dataclass(frozen=True)
class ElfInfo:
    machine: int
    interpreter: str | None
    needed: tuple[str, ...]
    search_paths: tuple[str, ...]


def read_elf(stream: BinaryIO) -> ElfInfo:
    def read_at(offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or length > 1024 * 1024:
            raise ValueError("Invalid ELF metadata bounds")
        stream.seek(offset)
        value = stream.read(length)
        if len(value) != length:
            raise ValueError("Truncated ELF metadata")
        return value

    header = read_at(0, 64)
    if header[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected a 64-bit little-endian Linux ELF binary")
    machine = struct.unpack_from("<H", header, 18)[0]
    phoff = struct.unpack_from("<Q", header, 32)[0]
    phsize, phnum = struct.unpack_from("<HH", header, 54)
    if phsize != 56 or not 0 < phnum <= 256:
        raise ValueError("Invalid ELF program header table")
    segments = [struct.unpack("<IIQQQQQQ", read_at(phoff + i * phsize, 56)) for i in range(phnum)]
    interpreter = None
    dynamic = []
    for kind, _, offset, _, _, size, _, _ in segments:
        if kind == 3:
            interpreter = read_at(offset, size).rstrip(b"\0").decode("utf-8")
        elif kind == 2:
            data = read_at(offset, size)
            if len(data) % 16:
                raise ValueError("Invalid ELF dynamic table length")
            for i in range(0, len(data), 16):
                tag, value = struct.unpack_from("<qQ", data, i)
                if tag == 0:
                    break
                dynamic.append((tag, value))
    strings = b""
    address = next((v for t, v in dynamic if t == 5), None)
    length = next((v for t, v in dynamic if t == 10), 0)
    if address is not None:
        for kind, _, offset, virtual, _, size, _, _ in segments:
            if kind == 1 and virtual <= address < virtual + size:
                strings = read_at(offset + address - virtual, length)
                break
        if not strings:
            raise ValueError("Invalid ELF dynamic string table")

    def string_at(index: int) -> str:
        if index >= len(strings) or b"\0" not in strings[index:]:
            raise ValueError("Invalid ELF dynamic string offset")
        return strings[index:].split(b"\0", 1)[0].decode("utf-8")

    return ElfInfo(machine, interpreter,
                   tuple(string_at(v) for t, v in dynamic if t == 1),
                   tuple(string_at(v) for t, v in dynamic if t in (15, 29)))


def host_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise ValueError(f"Unsupported Linux architecture: {machine}")


def host_libc() -> str:
    if Path("/etc/alpine-release").exists() or platform.libc_ver()[0] == "musl":
        return "musl"
    try:
        probe = subprocess.run(["ldd", "--version"], capture_output=True, text=True, timeout=5)
        if "musl" in (probe.stdout + probe.stderr).lower():
            return "musl"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "glibc"


def check_elf(info: ElfInfo, arch: str, libc: str, *, portable: bool = False,
              host: bool = False, addon: bool = False) -> None:
    if info.machine != MACHINES[arch]:
        raise ValueError(f"WRONG ARCH: ELF machine {info.machine}; expected {arch}")
    if portable and any("/nix/store/" in p for p in (info.interpreter or "", *info.search_paths, *info.needed)):
        raise ValueError("Non-portable ELF: Nix store loader or library search path; rebuild with an explicit Bun target")
    expected = LOADERS[arch, libc]
    if not addon and info.interpreter != expected:
        raise ValueError(f"Wrong Linux loader: {info.interpreter or '(none)'}; expected {expected} ({libc}). Select the matching libc release asset")
    if addon:
        is_musl = any("musl" in n or n == "libc.so" for n in info.needed)
        if is_musl != (libc == "musl"):
            raise ValueError(f"Native addon libc mismatch: expected {libc}, dependencies are {info.needed}")
    if host and info.interpreter and not os.path.exists(info.interpreter):
        raise ValueError(f"Missing ELF loader: {info.interpreter}. The binary exists, but Linux cannot start it. Install the host loader (on NixOS configure nix-ld), or use the matching glibc/musl asset")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--arch", choices=MACHINES)
    parser.add_argument("--libc", choices=("glibc", "musl"))
    parser.add_argument("--portable", action="store_true")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--addon", action="store_true")
    args = parser.parse_args()
    try:
        with args.binary.open("rb") as stream:
            info = read_elf(stream)
        check_elf(info, args.arch or host_arch(), args.libc or host_libc(),
                  portable=args.portable, host=args.host, addon=args.addon)
    except (OSError, ValueError, struct.error, UnicodeDecodeError) as exc:
        print(f"ELF compatibility check failed: {args.binary}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
