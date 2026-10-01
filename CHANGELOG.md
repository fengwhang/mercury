# Mercury changelog

This is Mercury's product history. Vendored upstream changelogs document
their original projects and are not Mercury release announcements.

## [Unreleased]

### Changed
- Use mLounge and MIRC implementation names while preserving installed configuration and extension aliases.

### Fixed
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
