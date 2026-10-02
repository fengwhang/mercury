# Mercury changelog

This is Mercury's product history. Vendored upstream changelogs document
their original projects and are not Mercury release announcements.

## [Unreleased]

## [0.3.8] — nightly

- Scope setup reasoning choices to each model's serving-provider metadata,
  respect mandatory reasoning and API defaults, and distinguish an omitted
  effort selector from an explicit unrestricted one. Configure second-order
  fallback effort and carry each selection through both engines' retry chains.

- Let setup choose a provider independently for each main and delegate
  fallback, including second-order fallbacks, with matching model catalogs
  and pricing. Preserve saved provider choices on reconfiguration.

## [0.3.7] — stable

- Build portable OMP runtimes for glibc and musl on Linux x64 and ARM64,
  removing build-host Nix loader dependencies.
- Select release downloads by CPU and libc, isolate musl native caches, and
  package only the matching fallback addons.
- Reject non-portable ELF loaders and native ABI mismatches before release;
  diagnose incompatible binaries before installing Python dependencies or
  swapping an existing installation.
- Resolve the nightly installer from its selected release tag.

## [0.3.6] — nightly

- Configure Hermes and OMP approval modes separately in setup; restore OMP's
  native tool tiers and remove Hermes smart risk review from OMP commands.
- Forward OMP child approval requests to the owner even when the Hermes
  parent uses YOLO; preserve live descendant policy and explicit deny rules.
- Make `!restart` in the managed MIRC gateway room restart the full
  Observatory, skipping mLounge when it is not installed.
- Probe quiet MIRC connections before declaring failure, preventing healthy
  progress output from triggering a false disconnect. Mark successful
  registration connected so real transport failures reach the recovery loop.
- Move `!spawnomp` startup and session RPC calls off the connection's event
  loop; keep heartbeats responsive and give repeated spawn names unique nicks.
- Reconnect idle agent identities automatically and serialize concurrent OMP
  recovery, preserving busy turns and honoring `!exit` during recovery.
- Retain spawned sessions and completed subagent rooms until explicit `!exit`,
  including descendants and history across transport reconnects.
- Checkpoint gateway sessions promptly during Observatory restart instead of
  waiting up to 30 minutes for active turns; shorten healthy bot verification.
- Synchronize membership probes after auto-join replies and filter by room,
  preventing false empty-room reports during restart verification.

## [0.3.5] — nightly

- Recognize mLounge's fork marker when checking the frontend focus fix,
  ignore obsolete bundle warnings, and label saved addresses before rotation.
- Report live gateway connections correctly in Observatory diagnostics, even
  when saved listener credentials are missing.
- Use Hermes's risk assessment for OMP smart-mode shell commands, allowing
  ordinary commands automatically and routing uncertain actions to the owner.
- Render mLounge Markdown with proper bulleted and numbered lists, nested
  blocks, and aligned, horizontally scrollable tables; keep tool traces
  plaintext and code commands literal.
- Recognize self-signed TLS connections protected by verified Tailscale or
  localhost in mLounge's connection indicator.
- Clarify that the Observatory login card's MIRC host is a bare hostname,
  with no `http://` prefix.
- Default the standard installer to stable regardless of inherited channel
  variables; explicitly select nightly in its wrapper and save the channel
  when creating the launcher, before optional setup steps.
- Honor the launcher's explicit update channel over stale saved markers,
  and refuse automatic downgrades when a channel's newest release is older.
- Accept prerelease tags so nightly installations can share a stable version.

## [0.3.4] — stable

- Promote the tested v0.3.4 nightly packages to stable, including the
  Observatory and shared approval improvements from v0.3.0–v0.3.3.

- Show the same delayed thinking kaomoji in OMP rooms and their descendants,
  using Hermes' existing face store. Preserve the indicator through tool and
  reasoning traces, and clear pending faces when a run ends or its room closes.

## [0.3.3] — nightly

- Make OMP user steering interrupt model output and continue within the same
  RPC run. Keep launched programs running, background tracked shell commands
  on steering, and deliver their results later. Tools that cannot safely yield
  finish before the correction is injected.
- Honor configured YOLO for recoverable tool and per-tool prompts in both
  engines and their descendants. Apply live policy changes across engine
  boundaries, avoid orphan approval mirrors, and align the default shared
  mode with Hermes. Explicit denials and provider confirmations remain enforced.

## [0.3.2] — nightly

- Recognize local and verified Tailscale connections in mLounge's protection
  indicator, preserving warnings for unprotected connections and invalid TLS
  certificates and explaining the problem in the tooltip.

## [0.3.1] — nightly

- Keep Observatory reconnects quiet: restore MIRC membership and initial topic
  metadata without repeated mLounge join/invite/topic notices, and post one
  "Observatory online - Mercury is back and ready" status per restored room.
- Fix topic replies that could disconnect clients joining rooms with a topic.

## [0.3.0] — nightly

### Changed
- Use mLounge and MIRC implementation names while preserving installed configuration and extension aliases.

### Fixed
- Long OMP approval commands retain every shell segment for parent policy checks.
- Live shared permission changes reach OMP descendants without restarting their rooms; explicit deny rules outrank allow rules.
- Web uploads reject files reached through symlinked credential or system directories.
- Source OMP runs use Mercury's version and changelog, matching compiled releases.
- OMP exposes only the task model identity; legacy role selectors cannot reroute workers.
- Nested OMP approvals reach the orchestrator UI and inherit its live permission policy.
- Hermes-to-OMP delegation retains approval context and background prompt routes.
- Observatory OMP rooms ask for non-shell approvals instead of rejecting them automatically.
- Both engines read shared safe/smart/yolo modes and YAML deny lists without deleting user OMP settings.
- Gateway lifecycle guards recognize Mercury service names and shell quote splicing.

## [0.2.23]

### Changed
- Name the web frontend mLounge and the agent chat layer MIRC, with upstream credits.
- Publish the rewritten README and Tailscale guide on the repository's main branch.

### Fixed
- Restore the login card's instructions for connecting another machine's mLounge
  to this machine's MIRC server over Tailscale, including MagicDNS and password location.

## [0.2.22]

### Changed
- Prefer MagicDNS names for tailnet-bound Observatory web login URLs.
- Simplify the web login card's connection instructions.

## [0.2.21]

### Added
- `mercury observatory login` and `mercury-nightly observatory login` reprint
  the setup card using the active installation's state.
- Explain Mercury's workflows, comparisons, and cross-device Tailscale setup.

## [0.2.20]

### Changed
- Promote the verified Observatory web frontend to stable.
- Use thermometer branding and Mercury's orange-red palette.
- Render tool/thinking traces as plaintext and assistant replies as Markdown/LaTeX.
- Replace separate raw-view/copy actions with one Raw toggle.
