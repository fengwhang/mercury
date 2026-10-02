# Portable Linux nightly releases

Mercury ships one compiled OMP executable per CPU and libc. GNU/glibc assets
retain `mercury-<version>-x64.tar.gz` and `mercury-<version>-arm64.tar.gz`;
musl assets use `mercury-<version>-musl-x64.tar.gz` and
`mercury-<version>-musl-arm64.tar.gz`. Each has a SHA-256 sidecar.

Bump and commit the Mercury version before building. Then stage the matching
GNU native addons and fetch the musl addons at the exact native package version:

```bash
python3 scripts/fetch-musl-natives.py
cd omp/packages/coding-agent
bun run build
CROSS_TARGET=linux-arm64 bun run build
CROSS_TARGET=linux-musl-x64 bun run build
CROSS_TARGET=linux-musl-arm64 bun run build
```

Run builds sequentially because their native archive and stats generators share
staging files. A native Linux build explicitly selects the official Bun target
rather than cloning the running Bun executable. This prevents a Nix-patched
build-host runtime from leaking its store-specific loader into the release.
The x64 runtimes use baseline CPU targets.

Musl native addons are staged under `omp/packages/natives/native/musl/`; they
must never replace the GNU files. The compiled musl runtime extracts its addons
into a separate cache subdirectory. The native package's internal version and
sentinel remain unchanged; the public executable reports the Mercury version.

Rebuild the Mercury TUI and mLounge payload, then run `bash scripts/make-dist.sh`.
The packer checks every selected executable before writing an archive: baked
Mercury version, ELF architecture, standard libc loader, absence of Nix store
search paths, and matching native addon ABI and version sentinel. Each archive
contains only its own CPU/libc fallback addons.

Before publishing, run `omp --version` and `omp --smoke-test` inside a clean GNU
Linux filesystem and an Alpine filesystem with no `/nix/store` mounted. Also
exercise a real local-provider tool turn to verify native operations. Minimal
chroots need `/proc` and `/dev` mounted, as a regular Linux runtime does. Alpine
requires `libstdc++`, `libgcc` and bash for shell tools. NixOS users need nix-ld.
Do not claim ARM64 runtime validation when only cross-compilation and ELF
inspection were performed. Publish nightly releases as prereleases and leave
existing stable and nightly assets immutable.
