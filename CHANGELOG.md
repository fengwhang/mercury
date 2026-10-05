# Mercury changelog

This is Mercury's product history. Vendored upstream changelogs document
their original projects and are not Mercury release announcements.

## [0.3.25] — stable

- The terminal backend selected during setup now applies to Hermes, OMP bash
  commands, OMP TUI shell commands, and all OMP descendants. Remote working
  directories resolve remotely, streaming output survives steering, and backend
  failures cannot silently run commands on the local host.
- SSH setup saves the current endpoint in shared config and correctly restores
  port 22 or clears an obsolete key path. Native engine approval policies remain
  independent. Dedicated OMP file tools, LSPs, eval, and hub programs remain local.

## [0.3.24] — stable and nightly

- Observatory restart preserves registered level-0 agents and the configured
  network's gateway room, expires all descendants and dead agents, and prevents
  cached mLounge joins from recreating orphan rooms such as an obsolete
  `#mercury_gateway` on a differently named network.
- Persist room destruction before removing every member; serialize JOIN
  notifications with deletion so late fanout cannot revive a deleted sidebar
  entry. Replayed cleanup journals cannot delete protected root rooms.
- Warn against Observatory restart during delegated work. Ordinary reconnects
  retain the established depth-based lifecycle, and mLounge remains optional.

## [0.3.23] — stable

- Prevent OMP completed reports submitted in the yield tool's error field
  from immediately aborting the task: request a corrected submission and
  advertise plain-text success reports, while preserving actual failures.

## [0.3.22] — stable and nightly

- Remove completed level-1 agent rooms and their descendants from MIRC and
  every connected mLounge browser. Wait for daemon confirmation, retry
  failed cleanup automatically, and prevent cached joins from reviving
  expired rooms. Deeper agents remain until their parent ends.
- Stream tool completion labels, full todo lists, delegation results,
  compaction, retries, fallback changes and extension notices into chat.
  Preserve plaintext tool output, hide Hermes reasoning by default, and
  retain OMP parent follow-up replies without duplicating final replies.
- Execute OMP room commands through their local harness. Keep `!model`
  available during active turns and after provider usage exhaustion;
  show failed-turn notices instead of an unexplained `(no output)`.

## [0.3.21] — stable and nightly

- Promote the verified v0.3.21 nightly packages to stable without rebuilding;
  both release channels use the same source revision and archive bytes.

- Fix `!model` in profile-spawned MIRC rooms when gateway multiplexing is
  disabled: read the active profile's inherited or explicit model settings
  instead of the startup home. Report invalid overrides without attaching
  the installation configuration file to the reply.

## [0.3.20] — nightly

- Inherit all four installation model slots in new profiles, including gateway
  model selection and fallback refresh. Keep native permissions/profile state local.
- Add `mercury profile models NAME` with the setup model, reasoning and context
  pickers; store complete overrides under `profile_models.NAME` in the main config.
  Invalid explicit settings fail instead of reverting to main models. Use
  `--inherit` to restore live inheritance. Both engines use the same authority.
- Preserve central model overrides across profile clone, rename and export/import;
  remove them on profile deletion. Unrelated settings saves never pin defaults.

## [0.3.19] — nightly

- Make Mnemosyne memory available by default in Hermes, preserving explicit
  opt-outs. Expose native memory tools with profile-aware instructions in both
  engines, and verify persisted memory contents before reporting success.
- Add `mercury memory remember` and `mercury memory recall`, including named
  profiles and nightly installations, without changing configuration.
- Bundle an engine-neutral Mnemosyne skill with native-tool and CLI guidance.
- Seed new profiles with stock Mercury skills; preserve a cloned profile's
  custom skills and deliberate removals. Keep skills and native OMP views
  profile-local across creation, clone and rename.
- Honor custom profile-local memory database paths consistently in runtime,
  CLI tools and status output.

## [0.3.18] — nightly

- Isolate Mnemosyne/Mnemopi databases per profile while sharing the profile's
  bank between Hermes and OMP. Rebase existing main-profile database pins
  at runtime, during setup and when rendering OMP settings.
- Keep the default profile's memories intact. Rebase profile creation,
  imports and renames; full-state clones snapshot SQLite, including live
  WAL contents, into an independent database.
- Preserve valid YAML when redacting exported profile configuration.
- Restore Return as a newline on mobile mLounge keyboards, including iOS.
  Use the visible Send button to submit; desktop Enter still sends.

## [0.3.17] — nightly

- Wait for MIRC JOIN confirmation before reporting an agent identity ready
  during Observatory startup and reconnection.
- Recheck missing identities for a bounded grace period after restart,
  preserving genuine failures without rejecting a late-joining agent.
- Verify the current live roster after resync, excluding tasks that end
  during the check. Treat incomplete NAMES replies as inconclusive.

## [0.3.16] — nightly

- Name delegated Observatory rooms after their immediate parent and inherit
  that parent's profile, including headless Hermes and nested OMP tasks.
- Keep gateway and manual spawns at level 0. End level-1 agents when their
  task completes; retain deeper agents until their parent exits, then tear
  down the entire descendant tree without reviving it from late events.
- Route approvals across Hermes/OMP descendants to the initiating level-0
  room, keeping OMP approval routes and event feeds active between turns.
- Recover from empty OMP last-turn yields and preserve greeting responses
  when the final assistant message contains only a yield call.

## [0.3.12] — nightly

- Select each model's context window immediately after its reasoning effort.
  Retain provider defaults and maxima separately; support single-window APIs,
  bounded custom limits, and automatic detection when metadata is unavailable.
- Enable built-in context compaction in every setup path. Add 50%, 75%, and
  custom percentages shared by both engines, with `mercury setup context`
  for direct configuration. Honor selected percentages over legacy autoraises.
- Clarify shared skill tool examples for both engines, including OMP task
  delegation, batch/result differences, and engine-specific prerequisites.
- Correct OMP formatting and stale Codex discovery-cache test fixtures so
  workspace checks pass.

## [0.3.11] — nightly

- Limit setup to default, fallback, delegate, and delegate fallback models,
  with reasoning immediately after each model selection. Always offer the
  main fallback independently of previously configured delegate models.
- Remove second-order setup menus; preserve extra retry models configured
  by hand and keep both engines' runtime fallback chains in sync.

## [0.3.10] — nightly

- Apply API-advertised reasoning efforts in both engines, including Codex
  discovery, mandatory reasoning, provider defaults, and models without an
  effort selector. Keep compatibility rules as the offline fallback.
- Give named profiles independent config/ prompt files for SOUL, AGENTS,
  HERMES, OMP, MEMORY, and USER. Preserve isolation during creation, clone,
  import/export, editing, and migration of existing profile instructions.
- Launch OMP under a named profile with `mercury omp -p NAME` or
  `!spawnomp AGENT -p NAME`, carrying model, effort, native permissions,
  instructions, and descendant configuration through Observatory restarts.
- Update the profile slash command, bundled and optional skills, helper
  scripts, and generated catalogs for Mercury's current configuration.
  Consolidate interviewing into `/grill-me` and archive replaced bundled
  copies only when unchanged, preserving user customizations.
- Keep OMP's single task model role and native permission modes separate
  from Hermes's approval modes. Harden test isolation against live service
  restarts.

## [0.3.9] — nightly

- Ask for reasoning immediately after each model selection in setup,
  including main, delegate, and second-order fallback models.

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
