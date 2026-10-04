# Contributing to Mercury

Mercury integrates its Hermes and OMP engine forks with shared configuration,
profiles, memory, skills, and the MIRC/mLounge Observatory. A change is complete
when it works through Mercury's entry points and preserves the other engine's
behavior. Testing a vendored engine in isolation is useful, but does not cover
the integration.

Read [README.md](README.md) for the product and [AGENTS.md](AGENTS.md) for
repository working rules. Read any applicable nested `AGENTS.md` before editing
that subtree. [PINS.txt](PINS.txt) records the upstream bases. The vendored
contributor guides describe their upstream projects; this guide describes
Mercury's development and release workflow.

## Find the right place to change

| Path | Responsibility |
| --- | --- |
| `bin/mercury` | Source launcher, environment composition, engine entry point |
| `hermes/mercury_cli/` | Mercury commands, setup, profiles, models, configuration, updates |
| `hermes/gateway/` | Gateway sessions, dispatch, user commands, restart behavior |
| `hermes/observatory/` | MIRC daemon, agent rooms, fleet lifecycle and diagnostics |
| `hermes/plugins/platforms/mirc/` | Hermes transport adapter for MIRC |
| `bridge/`, `hermes/tools/omp_*` | Hermes/OMP delegation, configuration and skills bridges |
| `omp/packages/coding-agent/` | OMP agent, tools, permissions, CLI and session behavior |
| `omp/packages/ai/`, `omp/packages/catalog/` | Provider implementations, model metadata and effort choices |
| `omp/packages/natives/`, `omp/crates/` | Native bindings and Rust implementations |
| `hermes/ui-tui/` | Hermes terminal interface |
| `third_party/mlounge/` | Observatory browser interface, Mercury's fork of The Lounge |
| `hermes/skills/`, `config/` | Bundled shared skills and default prompt/configuration files |
| `scripts/`, `install.sh` | Distribution builds, portability checks and installation |
| `docs/`, `CHANGELOG.md` | Design notes, reviews, operational guides and release history |

Search for the actual command or error, then follow its caller through the
launcher, profile resolution, engine and transport. Historical filenames can
still contain `irc` or `lounge`.

## Work in a branch and protect running installations

Create a branch from the agreed base. For a new contribution, that is usually
the current `main`; for an ongoing integration, use its current branch.

```bash
git clone https://github.com/fengwhang/mercury.git
cd mercury
git switch -c contribution/my-change
```

Keep changes in the source checkout. Installed trees under `~/.mercury` and
`~/.mercury-nightly` are user state and deployment targets, not development
checkouts. Do not restart a user's gateway or Observatory to test a source
change without authorization. Even an isolated home does not isolate systemd
service names or network ports; use an explicitly isolated service setup for
live integration tests.

[AGENTS.md](AGENTS.md) requires each writing subagent to have its own worktree
and branch, based on its parent's branch. Siblings must not share a writable
checkout or build on each other's unverified work. A single writer may use a
shared checkout on a non-main branch. Commit each verified slice; integrate
child branches through their parent. Never reset, clean, stash or revert
another contributor's worktree.

Agents should report the resulting behavior, verification and remaining
limitations. Publishing a release, deploying, or posting externally requires
authorization for that action; a request to edit code or documentation alone
does not authorize publication.

## Set up development without replacing your installed CLI

Use Python 3.11–3.13, `uv`, Node.js 22 or newer, and Bun compatible with
`omp/package.json` (currently at least 1.3.14). Native work also needs the Rust
toolchain in [omp/rust-toolchain.toml](omp/rust-toolchain.toml) and its build
dependencies. The OMP Nix flake is an optional development environment;
distributed binaries must still run outside that environment.

From the repository root, in a fresh development shell:

```bash
uv venv --python 3.13 "$HOME/.cache/mercury-dev/venv"
export MERCURY_PYTHON="$HOME/.cache/mercury-dev/venv/bin/python"
uv pip install --python "$MERCURY_PYTHON" -e './hermes[dev]' pytest-timeout

export MERCURY_REPO="$PWD"
export MERCURY_HOME="$(mktemp -d -t mercury-dev-home.XXXXXX)"
export MERCURY_CONFIG="$MERCURY_HOME/config.yaml"
export MERCURY_SKILLS_DIR="$MERCURY_HOME/skills"
export MERCURY_MEMORY_DIR="$MERCURY_HOME"
export MERCURY_INHERIT_FROM="$MERCURY_HOME/no-inherited-credentials"
export HERMES_PYTHON="$MERCURY_PYTHON"

./bin/mercury --print-env
./bin/mercury --help
```

Keep the virtual environment and development home outside the repository.
`MERCURY_INHERIT_FROM` above prevents first-run import of credentials from a
stock Hermes installation. Use test credentials only when a test needs them;
never commit `.env`, tokens, private configuration or session databases.
`HERMES_HOME` and the OMP state directory are composed by the Mercury launcher.
Install optional Python extras when the area you are testing needs them.

Install JavaScript dependencies for the components you will change:

```bash
(cd omp && bun install)
(cd hermes/ui-tui && npm install)
(cd third_party/mlounge && npm ci)
```

Review any lockfile changes. Avoid OMP's `bun run setup` for isolated work: it
also links the CLI globally. Use component build scripts instead. A local OMP
build can be made with `bun run build:native` from `omp/`, followed by
`bun run build` from `omp/packages/coding-agent/`. `HERMES_OMP_BIN` can select
a particular compiled executable for integration tests.

## Preserve Mercury's contracts

* **Models:** the shared `models` section is the editable authority for default,
  fallback, delegate model and delegate fallback. Preserve the complete provider
  identity, including model names containing `/`. Engine-specific settings are
  projections of that authority. OMP has exactly one model role: `task`.
  Do not restore upstream model roles or separate role-based effort settings.
* **Profiles:** an absent `profile_models.NAME` entry in the installation's main
  `config.yaml` means inheritance of its current main model settings. An explicit
  entry is authoritative; invalid configuration must fail clearly, rather than
  silently using main models. Both engines honor it. Profiles own their prompt
  files, local state and Mnemosyne bank, while inheriting installation models
  and credentials by default.
* **Provider capabilities:** effort and context choices must reflect the selected
  model/provider's supported options. Do not manufacture choices when metadata
  is unavailable or overwrite an existing setting when the user skips a picker.
* **Permissions:** Hermes and OMP have distinct native approval settings. Keep
  setup labels and configuration separate; OMP's `write` mode is not Hermes's
  `smart` policy. Respect each configured mode. Approval requests from descendants
  must reach the initiating level-0 agent's user interface, even across engines.
* **Sessions and delegation:** gateway agents and explicitly spawned agents are
  level 0. Children inherit their parent's profile and room naming prefix.
  Level-1 agents end on task completion; deeper descendants end with their
  parent. Preserve root sessions until `!exit` and resume them across service
  restarts. Gateway-room `!restart` restarts the Observatory even without an
  installed mLounge. Spawning, steering and approvals must not block the gateway
  transport or kill programs an agent has launched as a side effect.
* **Memory and skills:** both engines use the shared stock skill library. Keep
  instructions usable by either engine, identify any engine-specific tool names,
  and route memory operations to the active profile's bank.
* **Presentation:** use Mercury, mLounge and MIRC for product branding. Preserve
  real upstream credits, dependency/protocol names, and compatibility interfaces
  such as historical `IRC_*` keys and installed service paths. Render intended
  user replies as Markdown/LaTeX; preserve tools, commands and traces as plaintext.
  Use fenced code blocks for copyable commands in a user reply.

Changing a contract requires an explicit design decision, migration plan and
regression coverage. Do not silently change it while fixing another bug.

## Verify the change

Use the test runner for the affected component. Start with focused behavioral
tests; run broader checks when the change crosses boundaries. Tests should
exercise observable behavior, not merely assert that a source string exists.
Documentation-only changes normally need link and example validation, rather
than new implementation tests.

| Area | Commands, from the specified directory |
| --- | --- |
| Python / Mercury integration (`hermes/`) | `bash scripts/run_tests.sh tests/path/to/test_file.py`; `bash scripts/run_tests.sh` for the full suite |
| OMP (`omp/`) | `bun test packages/coding-agent/test/mercury-model-authority.test.ts`; `bun check` |
| Rust (`omp/`) | `bun run test:rs` and the applicable native build checks |
| Hermes TUI (`hermes/ui-tui/`) | `npm test`; `npm run check`; `npm run build` |
| mLounge (`third_party/mlounge/`) | `npm run test:vitest -- test/client/components/MarkdownBlocksTest.ts`; `npm test`; `npm run build` |
| Shell scripts (repository root) | `bash -n path/to/changed-script.sh` |

Run Python tests through [hermes/scripts/run_tests.sh](hermes/scripts/run_tests.sh),
not a direct `pytest` invocation. It isolates files in fresh processes and
cleans the environment. It prefers a checkout-local test virtual environment;
`HERMES_PYTHON` is its fallback when no suitable local environment exists.
Use OMP's Bun wrappers for TypeScript/Rust checks, not an ad hoc `tsc`, Biome
or raw Cargo workflow. Follow the subtree's formatting and type rules.

For integration fixes, cover the real entry point. Examples include the MIRC
adapter invoking `!model` in a freshly created profile, an approval traversing
a mixed-engine agent family, and a spawned agent appearing under its actual
parent's room. A direct helper test alone can miss context-scoping bugs.
Use fake providers and temporary homes where possible. Report real-provider,
browser, ARM hardware or live-transport checks only when actually performed.

## Submit a reviewable contribution

Keep a contribution focused. Describe the concrete trigger, previous behavior,
resulting behavior, tests run and any material compatibility implications.
Include a reproduction for a bug. Add a changelog entry for user-visible changes
when appropriate. Preserve upstream license notices and attribution.

Check `git diff --check` and inspect the final diff before committing. Separate
unrelated cleanup and dependency upgrades. If there is an existing test failure,
record its reproduction and distinguish it from failures caused by your change.
Do not claim a skipped or zero-test run passed. Use the normal repository review
workflow; agents must not create issues or comments without authorization.

## Release channels and ownership

The following procedures are for maintainers preparing an authorized release.
They describe manual commands; there is no root CI workflow that completes this
entire pipeline automatically.

| Channel | GitHub tag | Release state | Required assets | Installation home |
| --- | --- | --- | --- | --- |
| Nightly | `vX.Y.Z-nightly` | Prerelease, never GitHub Latest | 4 versioned archives + 4 checksums = 8 | `~/.mercury-nightly` |
| Stable | `vX.Y.Z` | Full release; mark Latest after verification | 4 versioned archives + 4 aliases + 8 checksums = 16 | `~/.mercury` |

The product version inside both channels is the bare `X.Y.Z`. Publishing a
nightly must not change the stable update channel or its installation marker.
Nightlies may come from an integration branch; keep public `main` aligned with
the stable source and documentation through the normal integration workflow.

Use [GitHub CLI](https://cli.github.com/manual/) or equivalent authenticated
GitHub API calls. Authenticate through `gh auth login` or a securely supplied
`GH_TOKEN`; never put a token in a command, notes, commit or log. The examples
below use one Bash session at the repository root. Replace the example version
and notes path with the release you are preparing.

```bash
set -euo pipefail
release_repo=fengwhang/mercury
release_version=0.3.22  # Example next version, not a publication instruction.
release_notes=/absolute/path/to/release-notes.md
```

For a new build, follow steps 1–4. To promote an existing nightly, set these
variables to its version, skip step 1, define the staging helper in step 2,
then follow [promotion](#promote-a-nightly-to-stable-without-rebuilding) and
steps 3–4. Do not run the new-nightly staging example during promotion.

### 1. Commit the version, then build

```bash
bash scripts/bump-version.sh "$release_version"
```

Update [CHANGELOG.md](CHANGELOG.md), review, and commit before compiling. The
bump helper updates Mercury's CLI version, OMP product identity and mLounge
package metadata, including its lockfile. Internal OMP/native package versions
such as `18.x` describe upstream ABI/dependency compatibility; do not bulk-bump
them to the Mercury version.

Require a clean source tree, then record the exact source commit:

```bash
test -z "$(git status --porcelain)"
release_commit=$(git rev-parse HEAD)
```

The packer uses `git archive HEAD`, so uncommitted source is absent from the
distribution. OMP bakes its version at compile time: **bump → commit → build →
pack**, never build and then bump. Rebuild affected artifacts if source changes.

Stage GNU native addons for both architectures at the exact version in
`omp/packages/natives/package.json`. Matching upstream npm leaf packages are
`@oh-my-pi/pi-natives-linux-x64@VERSION` and
`@oh-my-pi/pi-natives-linux-arm64@VERSION`; `npm pack` can retrieve them into a
temporary directory. Place their `.node` payloads under
`omp/packages/natives/native/`. Include the baseline x64 addon. Preserve their
licenses, and inspect the package contents and checksums. Do not fetch `latest`
or substitute a different native version. A native-source build is also valid
if its ABI, version sentinel and portability satisfy the packaging checks.

Fetch the separately staged musl addons, then compile all four OMP targets
**sequentially**, because their archive generators share staging files:

```bash
python3 scripts/fetch-musl-natives.py
(
  cd omp/packages/coding-agent
  CROSS_TARGET=linux-x64 bun run build
  cp dist/omp-linux-x64 dist/omp
  CROSS_TARGET=linux-arm64 bun run build
  CROSS_TARGET=linux-musl-x64 bun run build
  CROSS_TARGET=linux-musl-arm64 bun run build
)
(cd hermes/ui-tui && npm run build)
bash scripts/build-mlounge-fork.sh
bash scripts/make-dist.sh

for suffix in x64 arm64 musl-x64 musl-arm64; do
  test -s "dist/mercury-$release_version-$suffix.tar.gz"
  test -s "dist/mercury-$release_version-$suffix.tar.gz.sha256"
done
```

An x64 Linux host's plain `bun run build` also produces `dist/omp`; explicitly
selecting `linux-x64` produces `dist/omp-linux-x64`, hence the copy above. Use
the official Bun target and baseline x64 runtime. A Nix-specific ELF interpreter
or search path must never leak into a release.

The packer checks versions, native sentinels, CPU/libc compatibility and the
mLounge source fingerprint. It can **skip missing cross-builds**, so separately
require all four outputs. Inspect `DIST_INFO.txt` in each archive for the
expected source commit, version and platform. See
[Portable Linux releases](docs/linux-release-builds.md) for ABI and runtime checks.

Before publishing, test the packaged binaries with `--version`, `--smoke-test`
and a local-provider tool turn exercising shell/file operations. Test in clean
glibc and Alpine environments without the build host's `/nix/store`. Alpine
needs `libstdc++`, `libgcc` and bash; NixOS needs nix-ld. Cover installation,
stable/nightly channel selection, profile model inheritance/overrides, memory
isolation, approvals and Observatory dispatch with focused regression checks.
State explicitly if ARM64 received only cross-build/ELF inspection.

### 2. Stage correctly named assets and checksums

The packer emits bare-version names. A nightly needs `-nightly` in each archive
name. Stable additionally needs versionless aliases for all four platforms:

| Platform | Versioned stable archive | Stable alias |
| --- | --- | --- |
| x64 / glibc | `mercury-X.Y.Z-x64.tar.gz` | `mercury-x64.tar.gz` |
| ARM64 / glibc | `mercury-X.Y.Z-arm64.tar.gz` | `mercury-arm64.tar.gz` |
| x64 / musl | `mercury-X.Y.Z-musl-x64.tar.gz` | `mercury-musl-x64.tar.gz` |
| ARM64 / musl | `mercury-X.Y.Z-musl-arm64.tar.gz` | `mercury-musl-arm64.tar.gz` |

Each archive needs its own `.sha256` file containing its **published basename**.
Do not simply rename a checksum sidecar. The packer's local alias sidecars can
be hardlinks containing the versioned filename; generate fresh sidecars in a
new staging directory. This helper copies archive bytes without rebuilding:

```bash
stage_release_assets() {
  local source_dir=$1 source_version=$2 destination_version=$3 aliases=$4
  local suffix source_file archive_name alias_name
  release_stage=$(mktemp -d -t mercury-release-assets.XXXXXX)
  for suffix in x64 arm64 musl-x64 musl-arm64; do
    source_file="$source_dir/mercury-$source_version-$suffix.tar.gz"
    archive_name="mercury-$destination_version-$suffix.tar.gz"
    test -s "$source_file"
    cp "$source_file" "$release_stage/$archive_name"
    (cd "$release_stage" && sha256sum "$archive_name" > "$archive_name.sha256")
    if [ "$aliases" = true ]; then
      alias_name="mercury-$suffix.tar.gz"
      cp "$source_file" "$release_stage/$alias_name"
      (cd "$release_stage" && sha256sum "$alias_name" > "$alias_name.sha256")
    fi
  done
}
```

For a new nightly built in step 1:

```bash
release_tag="v$release_version-nightly"
release_is_nightly=true
stage_release_assets dist "$release_version" "$release_version-nightly" false
```

For a stable built directly from reviewed source, use destination version
`"$release_version"` and `true` for aliases, with `release_is_nightly=false`.
Promotion of an already verified nightly is preferred when those exact builds
are ready for stable.

### Promote a nightly to stable without rebuilding

Keep the original nightly tag, prerelease status and assets unchanged. Download
its published archives and sidecars, verify the downloaded checksums, and use
the nightly tag's exact commit for the new stable tag:

```bash
nightly_tag="v$release_version-nightly"
git fetch origin "refs/tags/$nightly_tag:refs/tags/$nightly_tag"
release_commit=$(git rev-parse "$nightly_tag^{commit}")
nightly_download=$(mktemp -d -t mercury-nightly-download.XXXXXX)
gh release download "$nightly_tag" --repo "$release_repo" \
  --pattern "mercury-$release_version-nightly-*.tar.gz*" \
  --dir "$nightly_download"
for suffix in x64 arm64 musl-x64 musl-arm64; do
  (cd "$nightly_download" && \
    sha256sum --check "mercury-$release_version-nightly-$suffix.tar.gz.sha256")
done

release_tag="v$release_version"
release_is_nightly=false
stage_release_assets "$nightly_download" "$release_version-nightly" \
  "$release_version" true
```

Verify these downloads against the nightly's GitHub asset digests as well.
The four stable archives and their aliases must have the exact same SHA-256
hashes as the corresponding nightly archives. Only filenames and checksum
sidecars change. A code fix discovered during promotion needs a new reviewed
build/release, not replacement of an existing nightly asset.

### 3. Create a draft, upload everything, verify, then publish

Push the reviewed source branch when preparing a new build, and create an
exact-commit tag. For promotion, its source commit should already be public.
Never move an existing release tag or overwrite a published asset.

```bash
git tag "$release_tag" "$release_commit"
git push origin "refs/tags/$release_tag"

if [ "$release_is_nightly" = true ]; then
  gh release create "$release_tag" --repo "$release_repo" --verify-tag \
    --draft --prerelease --latest=false --title "Mercury $release_tag" \
    --notes-file "$release_notes"
else
  gh release create "$release_tag" --repo "$release_repo" --verify-tag \
    --draft --latest=false --title "Mercury $release_tag" \
    --notes-file "$release_notes"
fi
gh release upload "$release_tag" --repo "$release_repo" \
  "$release_stage"/*.tar.gz "$release_stage"/*.tar.gz.sha256
```

The [create](https://cli.github.com/manual/gh_release_create) and
[upload](https://cli.github.com/manual/gh_release_upload) commands support this
draft-first workflow. If interrupted, resume the existing draft: compare each
existing asset's size and hash, then upload only missing files. Do not use
`--clobber` to bypass an unexplained mismatch. A published release is immutable.

Check the exact asset set, sizes and server-reported SHA-256 digests before
making the draft public:

```bash
gh api "repos/$release_repo/releases/tags/$release_tag" \
  > "$release_stage/remote-release.json"
python3 - "$release_stage" "$release_is_nightly" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

stage = Path(sys.argv[1])
expected_count = 8 if sys.argv[2] == "true" else 16
local = {p.name: p for p in stage.glob("*.tar.gz*")}
release = json.loads((stage / "remote-release.json").read_text())
remote = {a["name"]: a for a in release["assets"]}
assert release["draft"] is True, "Expected an unpublished draft"
assert len(local) == expected_count and local.keys() == remote.keys(), "Asset set mismatch"
for name, path in local.items():
    with path.open("rb") as stream:
        digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    assert remote[name]["size"] == path.stat().st_size, name
    assert remote[name].get("digest") == digest, f"Missing or incorrect digest: {name}"
print(f"Verified {expected_count} uploaded assets")
PY
```

If GitHub has not supplied a digest yet, wait and recheck, or independently
download and hash the uploaded asset; do not skip verification. Also compare
published checksum contents, inspect the release notes and confirm the remote
tag resolves to `release_commit`. Notes should explain behavior, validation,
platform coverage and limitations without private logs or credentials.

Only after those checks, [publish the draft](https://cli.github.com/manual/gh_release_edit):

```bash
if [ "$release_is_nightly" = true ]; then
  gh release edit "$release_tag" --repo "$release_repo" \
    --draft=false --prerelease --latest=false
else
  gh release edit "$release_tag" --repo "$release_repo" \
    --draft=false --prerelease=false --latest
fi
```

[scripts/upload-dist.sh](scripts/upload-dist.sh) is a legacy helper covering
only the two glibc architectures. It does not implement this four-platform
draft, checksum and publication gate. Do not use it as the complete release
procedure. OMP's upstream `bun run release` is also not Mercury's distribution
publisher.

### 4. Check the public release and update paths

After publication, verify all archive and checksum URLs, tag/source identity,
prerelease/Latest flags and asset hashes again. Stable's
`releases/latest/download/mercury-PLATFORM.tar.gz` aliases and sidecars must work
for every platform. Verify that `mercury update` selects stable and
`mercury-nightly update` selects the intended nightly, preserving their separate
homes and channel markers. Publishing stable must not silently move nightly
users to another build.

For stable, integrate the reviewed source and update the public README/changelog
on `main` through the normal review workflow. Reconcile any independent main
changes; never force-push over them. Confirm the files visible on GitHub actually
describe the stable release. Documentation-only follow-up commits may be newer
than the immutable release tag; the tag must still identify the packaged source.

Record the release URL, source commit, asset hashes and validation results.
Confirm earlier releases remain unchanged. Tell users the appropriate channel's
update/restart commands; do not restart a live installation merely to finish
publishing.
