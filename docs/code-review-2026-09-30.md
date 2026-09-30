# Mercury integration code review — September 30, 2026

Reviewed revision: `37248f1d` (v0.2.20). This review found **eight actionable
issues**, including two high-priority integrity/policy failures. The README
was rewritten against the implementation and current primary project docs.
The findings below are open; this documentation change does not fix them.

## Coverage and method

The review covered Mercury's install/release scripts and launcher, the
configuration bridge, provider credential sharing, profile/state boundaries,
OMP delegation and RPC approvals, scheduled OMP execution, Observatory
provisioning/identity/room routing, IRC message transport, Lounge rendering,
uploads, and the web setup path. It combined source inspection with the
existing suites and synthetic reproductions, including a local TCP peer for
the standalone IRC sender and the real Lounge input handler/framework splitter.

This is a repository-wide integration review with deeper inspection of
Mercury's own changes and their call sites. It is not a line-by-line audit of
every vendored upstream tool, a dependency vulnerability audit, or an end-to-end
paid-provider/tailnet deployment test. No real provider credentials were used
in the reproductions.

## Findings

### 1. [P1] Release updates proceed when checksum verification cannot complete

Location: [update_release.py](../hermes/mercury_cli/update_release.py#L522),
particularly the exception handler around lines 530–531.

If the published sidecar download times out, cannot be read, or contains no
checksum, the updater prints a warning and continues to extract and replace
the installation. A missing sidecar also skips verification entirely. This
contradicts the module's integrity contract and makes transient checksum
failures sufficient to install unverified bytes. A mismatch is rejected, but
an inability to verify must also be rejected.

Reproduction: a layout-valid synthetic release tarball plus a sidecar download
that raises `OSError` reaches `_swap_tree`:

```text
UPDATE 0.2.19 -> 0.2.20; checksum_failure=True; swap_reached=True
checksum step failed (...) — continuing without it
```

Recommended fix: require a valid expected digest for official release updates,
verify it before parsing/extracting the archive, and leave the installed tree
untouched on any sidecar failure. Apply the same required-sidecar rule to
[install.sh](../install.sh#L373), which currently treats the sidecar as optional.

### 2. [P1] Valid YAML deny lists can disappear in OMP's policy

Location: [bridge.py](../bridge/bridge.py#L398).

`_hermes_deny_globs` recognizes only a bare `deny:` followed by more-indented
`- item` lines. Valid flow-style YAML such as the following is read correctly
by Hermes' YAML loader but returns no deny rules from the bridge:

```yaml
hermes:
  approvals:
    deny: ["*git push*"]
```

Rendering that file succeeds and emits no `omp.bash.patterns`. The
[`mercury omp` launcher](../hermes/mercury_cli/omp_command.py#L67) calls this
renderer before starting OMP, whose
[native Bash approval check](../omp/packages/coding-agent/src/tools/bash.ts#L559)
reads those patterns. Direct OMP therefore loses the explicit deny regardless
of whether Hermes correctly enforces it elsewhere. Under `approvals.mode: off`,
the missing custom deny leaves ordinary matching commands eligible to run.

Reproduction: the probe reports the same effective YAML list for both spellings,
but the bridge returns `[]` for the flow list and `['*git push*']` for the block
list. The rendering probe confirms that the flow list produces no Bash policy.

Recommended fix: parse the shared YAML once with the same effective schema as
the runtime; translate every accepted deny-list representation. Reject invalid
policy types rather than silently interpreting them as an empty policy.

### 3. [P2] Promoting a release can make nightly updates downgrade it

Location: [update_release.py](../hermes/mercury_cli/update_release.py#L439).

The nightly selector takes the first remaining prerelease. Once installed
v0.2.20 is promoted to stable, that can be v0.2.19. The version check uses
`latest <= current`, then treats a different tarball digest as permission to
update even when the selected release is strictly older. Different versions
normally have different digests, so the same-tag repair mechanism becomes an
implicit downgrade mechanism.

Reproduction: an installed 0.2.20, a selected 0.2.19 release, and different
fixture digests reach the code-swap boundary; the output incorrectly calls
them “same tag, different bytes.”

Recommended fix: reject strictly older versions before probing content. Apply
the digest repair only to equal versions. Decide explicitly whether the nightly
selector should include promoted releases or simply report the installed newer
version as current.

### 4. [P2] Rendering the bridge deletes unrelated OMP settings

Location: [bridge.py](../bridge/bridge.py#L568).

The renderer replaces the entire `omp:` subtree with a generated block. It
preserves a few selected memory settings but removes other user settings,
including themes, editor configuration, MCP-related settings, and custom tool
configuration. Rendering is triggered by normal launches/delegation as well as
`omp-sync`; these are not requests to reset the user's configuration.

Reproduction: render a temporary valid config containing `omp.theme.dark:
custom` and an editor setting. Rendering exits successfully, but neither key
remains. The committed probe reports `theme_preserved=False`.

Recommended fix: merge only the bridge-owned leaves into the existing subtree,
preserving other keys and custom policy entries. Write atomically, with a clear
rule for any managed leaves the bridge deliberately overrides.

### 5. [P2] Pasted source is changed on the Lounge → agent path

Locations: [Lounge msg.ts](../third_party/thelounge/server/plugins/inputs/msg.ts#L119),
[adapter.py tag parsing](../hermes/plugins/platforms/irc/adapter.py#L1077), and
[batch closing](../hermes/plugins/platforms/irc/adapter.py#L1183).

The Lounge drops empty lines before opening a multiline batch. Its use of
`irc-framework.say()` also wraps long logical lines into several physical
frames with the same batch tag, without `draft/multiline-concat`. The adapter
reads only the batch reference and joins all payloads with `\n`, even when a
proper client supplies a concat tag. Empty paragraphs disappear and long code
lines gain newlines before the model receives the text.

Reproduction: the real Lounge handler, with a 40-byte test chunk budget, drops
the blank line in `first\n\n<100 a characters>\nlast` and emits three ordinary
batch-tagged fragments for the long line. Separately, feeding the adapter
`abc` plus a concat-tagged `def` delivers `abc\ndef` instead of `abcdef`.

Recommended fix: use the same lossless UTF-8 framing semantics as the outbound
agent sender. Represent blank lines explicitly, tag continuations, and teach
ingress reassembly to honor both tags. Test from the web input handler through
adapter dispatch, including source with indentation and long lines.

### 6. [P2] Scheduled IRC replies still corrupt Markdown and shell commands

Location: [adapter.py](../hermes/plugins/platforms/irc/adapter.py#L1740).

`_standalone_send`, used by cron/out-of-process delivery, still calls the old
regex Markdown stripper, sends ordinary untyped `PRIVMSG`s, removes blank lines,
and trims chunk boundaries. It bypasses the new provenance and multiline path.
The stripping operates inside code fences, so this changes code itself, not
just its visual formatting. The Lounge cannot recover the original source.

Reproduction over a local TCP IRC peer: the fenced command
`printf "%s" "$HOME" **/*.py` is sent as
`printf "%s" "$HOME" */.py`; the fence also leaves stray backticks and `sh` in
the wire messages. The sender nevertheless reports success.

Recommended fix: reuse the provenance-aware batch encoder for standalone
replies. Negotiate capabilities, preserve code and whitespace, and explicitly
mark assistant replies. Define a lossless plaintext fallback for peers without
multiline support.

### 7. [P2] Upload denial checks lose protected paths through symlinks

Location: [lounge.py](../hermes/observatory/lounge.py#L846).

`stage_lounge_upload` resolves a source before checking its path. If an
operator's `.ssh` directory is a symlink to an external directory, an input
like `~/.ssh/id_ed25519` resolves outside the protected home subtree; its
basename also avoids the `.key`/`.pem` checks. The file is then copied into the
web upload directory. System files backed by symlinks can bypass the `/etc`
restriction for the same reason; this is why the `/etc/hostname` refusal test
fails on the review host's NixOS layout.

Reproduction: a temporary operator home with `.ssh` pointing to a temporary
external directory containing a synthetic `id_ed25519` is accepted and staged.
Only synthetic bytes were used.

Recommended fix: enforce protected-path checks on both the normalized requested
path and the resolved target, and resolve protected home roots consistently.
Open/check/copy the source with race-aware handling if the denial policy is
intended as a security boundary.

### 8. [P2] Stable and nightly Observatories overwrite the same user services

Locations: [provision.py](../hermes/observatory/provision.py#L622),
[config_gen.py](../hermes/observatory/config_gen.py#L33), and
[lounge.py](../hermes/observatory/lounge.py#L601).

Stable and nightly have separate state homes, but both provision the same
`~/.config/systemd/user/mercury-observatory.service` and
`mercury-lounge.service`, and use the same default ports. The IRC unit is
rewritten and restarted unconditionally. Setting up nightly under the same
Linux user can therefore replace the stable daemon's unit and move it onto the
nightly state tree. Separate homes alone do not isolate the running services.

Evidence: source inspection of both unit writers, their constant names, and the
restart calls; no real systemd services were changed to demonstrate this issue.

Recommended fix: namespace service identities and ports by installation/channel,
or explicitly support a single shared Observatory with a defined owner. The
README now recommends one Observatory installation per Linux user.

## Validation results

| Check | Result |
| --- | --- |
| Hermes canonical runner, 74 selected files | **665 passed, 4 failed, 1 skipped** |
| OMP/Bun, seven integration/auth/approval/RPC files | **52 passed, 0 failed** |
| Full Lounge Vitest suite | **328 passed, 1 failed** |
| Bridge standalone checks | **39 passed, 2 failed** |
| Synthetic review probes | Reproduced findings 1–7; finding 8 verified in source |

Python checks ran on an isolated Python 3.13 environment with the installed
runtime dependencies available read-only. An initial Python 3.11 run could not
load several 3.13 native dependencies; its results were discarded in favor of
the compatible run above.

The four Python failures are:

- `test_launcher_delegates_to_argparse_entrypoint`: expects a removed
  `hermes/mercury` launcher instead of the distribution's `bin/mercury`.
- `test_resync_subscribes_lobby`: patches the now-async purge-journal function
  with a synchronous lambda returning a list.
- `test_stage_upload_refusals`: exposes the protected-path symlink issue in
  finding 7 on NixOS.
- `test_new_channel_auto_joins_server_clients`: IRC receive timeout; this was
  also reproduced on the earlier v0.2.16 baseline during the previous work.

The Lounge failure expects the web manifest name to remain `The Lounge` even
though the published branding intentionally changed it to `Mercury`.
The two bridge failures expect missing fallback models to be rejected, while
the current product deliberately allows optional fallbacks. Those stale
expectations should be updated separately from runtime fixes.

The OMP run includes real Python/Bun credential IPC, OAuth account selection
and refresh races, credential-read deny rules, local RPC slash commands, and
Mercury branding. The Python run includes Observatory transports and room
control, state/profile isolation, provider sync, credential removal, release
tracks, update guards, OMP RPC/skills, deny rules, and scheduled RPC execution.
Missing migration test paths matched no files and were not counted as tested;
those migration modules received source review.

## Reproducing the findings safely

The committed [Python probes](review-probes-2026-09-30.py) use temporary state,
fake release metadata, synthetic credentials, and a local TCP peer. They stop
the updater at a mocked swap boundary. Run with a Python environment containing
the Hermes runtime dependencies:

```bash
python docs/review-probes-2026-09-30.py
```

The [Lounge input probe](review-input-probe-2026-09-30.ts) calls the real input
handler with a disconnected IRC client. From the Lounge tree, with its
development dependencies installed:

```bash
cd third_party/thelounge
npx tsx ../../docs/review-input-probe-2026-09-30.ts
```

The probes print the observed incorrect behavior; they are diagnostic
reproductions rather than a passing regression suite. Add assertions for the
intended behavior alongside each runtime fix.

## README changes and evidence

The pitch now describes the maintained combination of persistent Hermes work,
OMP coding, shared knowledge/credentials, and self-hosted addressable agent
rooms. It explains child-context boundaries, transport-dependent steering,
Linux-only published bundles, Node/npm requirements, and actual command names.
It removes unsupported claims of universal platform support, guaranteed coding
delegation, complete CLI parity, and perfect cross-engine policy equivalence.

The competitor comparison acknowledges existing upstream capabilities and links
the checked primary documentation:
[OpenClaw multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent),
[Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/),
[Claude Code teams](https://code.claude.com/docs/en/agent-teams) and
[Remote Control](https://code.claude.com/docs/en/remote-control),
[Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[remote access](https://learn.chatgpt.com/docs/remote), and
[scheduled tasks](https://learn.chatgpt.com/docs/automations), and
[OMP's README](https://github.com/can1357/oh-my-pi).

The Tailscale guide follows Mercury's existing wizard: two separate bind choices,
Lounge credentials, pre-seeded IRC network, port 9000, service persistence,
existing-localhost migration, and doctor/restart commands. External steps were
checked against [Tailscale installation](https://tailscale.com/docs/install) and
[Linux setup](https://tailscale.com/docs/install/linux). A real cross-device
login was not performed during this review.
