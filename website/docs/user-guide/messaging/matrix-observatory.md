# Matrix Observatory

The Matrix Observatory is Mercury's built-in Matrix UI: a small homeserver
that ships with Mercury and a sidecar that mirrors every live agent session
on your machine as a tree of Matrix rooms. You watch each agent work —
every tool call, streamed as it happens — and you can talk to any agent,
stop it, approve its tool use, or spawn new ones, all from the FluffyChat
app on your phone. Nothing leaves your machine: the homeserver is local,
registration is closed, and federation is off.

The tested recommendation is **FluffyChat** (iOS/Android/desktop). Other
Matrix clients work for reading; this guide assumes FluffyChat.

> **Status at time of writing.** The homeserver side is live: install-time
> provisioning, the Tuwunel binary, the owner account, and the
> `mercury-observatory-homeserver.service` systemd unit all work today.
> The **sidecar daemon** (`mercury-observatory.service`) that populates
> rooms, streams tool calls, and takes steering/commands — and with it
> **per-room E2EE** — are still **landing**. Until the sidecar ships, you
> can log in and see the empty server, but rooms won't fill up yet.
> Design and rollout state: `docs/design/matrix-observatory.md` (the
> truth doc) and `TODO.md` Track M.

## What gets installed

The observatory is provisioned by `install.sh` (skip with
`--skip-observatory`) and is **on by default**. Provisioning is idempotent
and fail-hard — re-running it never breaks an existing setup:

- **Tuwunel** — a single static Rust homeserver binary, fetched at install
  time from the latest stable upstream release (minimum 1.8.1). It is not
  vendored into the Mercury tarball; `mercury update` refreshes it.
- **A closed config** — `~/.mercury/observatory/tuwunel.toml`, written
  once: `server_name = mercury.local`, bound to `127.0.0.1:18008`,
  federation off, registration off. The file is hand-editable and never
  overwritten; delete it to re-provision from scratch.
- **An owner account** (`@owner:mercury.local`) with a random password,
  stored at `~/.mercury/observatory/owner-credentials.json` (mode 0600).
- **A systemd user unit** — `mercury-observatory-homeserver.service`,
  enabled at install, logs under `~/.mercury/observatory/logs/`.

Everything lives under `~/.mercury/observatory/` (binary, database,
config, credentials, logs) — nothing is written into the engine tree.

## First login with FluffyChat

You log in **once**, as the owner, with a manual homeserver URL:

1. Get your credentials:
   `cat ~/.mercury/observatory/owner-credentials.json`
2. In FluffyChat, add an account and enter the homeserver URL manually
   (choose the custom-homeserver option — not matrix.org — and paste
   the URL from step 3).
3. Homeserver URL: `http://127.0.0.1:18008` when the app runs on the
   same machine. From your phone, reach the server through your existing
   VPN (e.g. `tailscale serve` proxying port 18008), or edit `address`
   in `tuwunel.toml` to bind your tailnet/LAN IP and restart the unit:
   `systemctl --user restart mercury-observatory-homeserver.service`.
   Do not expose the homeserver beyond your VPN — it has no per-user
   rate limiting by design.
4. Username `owner`, password from the credentials file.

Other humans are not part of the default picture: the server is
single-owner and closed. (The design supports invited users with
read-only or steer-level power in specific rooms — write access to an
agent's room *is* the authority to steer that agent.)

## The space tree

Everything the observatory shows lives in one Matrix space:

```
Mercury — <hostname>            (the gateway space, root)
├─ Gateway room                 your main conversation + the root dashboard
├─ Directives                   broadcast room — see below
├─ cron:<job>                   one room per cron job, rolling history per fire
└─ <orchestrator> subspace      one per /spawn or /spawnomp
   ├─ <name> room               that agent's conversation
   ├─ subagent rooms            one per delegate_task child, nested any depth
   └─ dashboard                 rolling "what's running" summary
```

Each agent — orchestrator, delegate child, or omp subagent at any depth —
gets its own room, named after the `name` it was spawned with. Rooms
stream one message **per tool call** (arguments truncated to ~200
characters; results elided except errors — full text stays in the local
transcripts). omp-side reasoning appears as separate quoted messages you
can toggle per room with `/cot on|off`. Every space carries a
rolling-edited dashboard with the live tree, agent count, and blocked
approvals.

## Steering and commands

Write in an agent's room and it reaches that agent as a steering message
at the next safe boundary — the room shows `⏳ queued` then `✔ applied`.
Plain prose steers; slash verbs control:

- `/stop` — ask the agent to stop (shows `🛑 stop requested — waiting for
  boundary` until the kill confirms). Steer it first if you want a wrap-up.
- `/status` — the agent posts its current state (running/settled, what
  it's doing, its position in the tree) as a fresh summary.
- `/cot on|off` — show/hide that room's omp thinking stream (default on).
- `/approve [once|session|always]` / `/deny` — reply these to an approval
  prompt in the agent's room to resolve it.
- `/spawn <name>` — create a new hermes-side orchestrator (indefinite
  lifespan, resumable; only `/exit` ends it).
- `/spawnomp <name>` — same, but an omp-side orchestrator (headless).
- `/exit` — kill a spawned orchestrator and annihilate its whole space
  (the local transcript survives).

Every verb also works with a `!` prefix (`!stop`) for clients that
reserve `/`. Any other `/command` passes through to the agent's native
command surface — the full hermes slash registry in hermes rooms, omp's
command handling in omp rooms. Process-level verbs like `/restart` and
`/update` are accepted only in the gateway room, never in agent rooms.

## The Directives room

The Directives room broadcasts to your top-level orchestrators (the
`/spawn` and `/spawnomp` crowd) — but only to the ones you **@-mention**:

- `@auth-refactor stop after the current test run` reaches exactly that
  agent; a directive with no valid member mention delivers nothing and
  the room replies with a short help note listing current members.
- `@room` / `@everyone` reaches every member.
- Agents never answer in the Directives room — replies land in each
  agent's own room. A rolling line tracks delivery per agent.

## What disappears, and when

Rooms are deleted by agent depth, never on a timer:

- **Direct children** (depth 1): their room and subspace are destroyed
  the instant the task completes — the final summary posts to the
  **parent's** room only.
- **Deeper agents** (depth ≥ 2): their rooms survive after they finish
  ("settled — transcript only", steering disabled) until their parent
  dies; parent death cascades the whole subtree.
- **Orchestrators** (depth 0): history survives until `/exit` or a
  session reset; `/exit` purges the entire subtree.
- **Restarts are not death.** Gateway restarts, Mercury updates, and
  homeserver/sidecar restarts never delete depth-0 rooms — live
  orchestrators are resumed and re-attached to the same rooms on the
  next start.

## The kill switch and updates

```yaml
observatory:
  enabled: true   # false: freezes the observatory — nothing is deleted
  offline: false  # true: never query GitHub for a new Tuwunel release
```

`observatory.enabled: false` stops all observatory activity and leaves
every room frozen in place; re-enabling resumes. `observatory.offline:
true` is for air-gapped machines: provisioning and updates trust the
already-downloaded binary instead of the network.

Both update surfaces — the `/update` command and shell `mercury update` —
refresh Tuwunel to the latest stable release (same ≥ 1.8.1 gate as
install) and restart `mercury-observatory-homeserver.service`. Both are
config-gated: disabled or offline means a silent skip, and a failed
refresh never blocks the Mercury update itself.

## Secondary chat (Matrix) — the other Matrix plugin

The classic single-account Matrix bot (bring-your-own homeserver and
access token) still ships, now labeled **Secondary chat (Matrix)** so it
can't be confused with the observatory. The two never interact: the
observatory owns its homeserver and virtual users; Secondary chat is your
personal account on some other server.
