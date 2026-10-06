# Gateway Monitoring

Mercury gateway health and operational diagnostics stay local. There is no
Langfuse plugin or gateway OTLP exporter, including when an older configuration
still contains exporter opt-ins or endpoints. No exporter SDK is installed by
monitoring startup or the tools setup catalog.

## Inspect local health

```bash
mercury monitoring status
mercury status
```

`monitoring status` reads the local gateway runtime-status file and cron state.
It reports gateway liveness and state, foreground activity, restart/drain state,
platform health, background-work counts, cron scheduler ages when available,
and enabled/running/overdue cron job counts. Missing cron state does not prevent
gateway health from being reported.

`mercury.gateway.active_agents` counts foreground message turns, in-flight cron
jobs, and API runs. `mercury.gateway.background_work` counts detached work,
including background terminal processes and task-granular async delegation.
`mercury.gateway.background_delegations` counts dispatch units instead of child
tasks, preserving the distinction between concurrent work and pool-slot usage.

The background counters describe the inspecting process's registries. A separate
CLI process cannot inspect another process's in-memory background registries.
Gateway runtime status and persisted cron state remain available across processes.

## Local diagnostics

Gateway runtime transitions and cron execution state still produce local,
content-free monitoring events. Gateway and error logs remain on disk, and
shared metrics remain in their local SQLite store and filesystem outbox. No
remote destination is attached by the gateway.

Health snapshots reduce platform errors to bounded error classes rather than
including arbitrary error text. They do not include prompts, messages, tool
arguments/results, job names, schedules, or detailed execution traces. Gateway
logs are the separate local surface for diagnostic detail.

The gateway diagnostic log handler and emitter are local signal producers.
Cron terminal execution events make a bounded, fail-open emitter flush attempt
of up to one second. This is local queue processing, not a remote upload.

## Existing configuration

Remove obsolete `monitoring.gateway_health_export` and `monitoring.export.otlp`
entries and Langfuse credentials from old configuration files. These settings
have no shipped reporting implementation or setup catalog entry. Existing
`plugins.enabled` entries for `langfuse` or `observability/langfuse` do not find
a bundled plugin. No compatibility exporter or no-op shim is retained.

`monitoring.install_id` remains an optional local correlation identifier. Local
health metrics hash that value rather than exposing the raw identifier.
Configured provider chat and memory-service requests are unrelated integrations
and are not changed by removal of reporting exporters.
