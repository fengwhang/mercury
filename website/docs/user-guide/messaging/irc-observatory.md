# IRC Observatory

The IRC Observatory is Mercury's built-in chat network: a small IRC
server that ships with Mercury where every live agent gets its own
channel. You watch each agent work — every tool call, streamed as it
happens — and you can talk to any agent, steer it mid-run, or spawn
new ones, all from any IRC client on your phone or desktop. Nothing
leaves your machine: the server binds localhost (phones reach the
bouncer over Tailscale), and there are no accounts — the bouncer
password is the auth.

Use any IRC client (desktop or mobile). History replay is built in:
the daemon keeps the last 200 messages per channel (persisted across
restarts) and replays them when your client joins, so a client that
disconnects and returns sees what it missed.

> **Status.** The observatory ships: install-time provisioning (config,
> passwords, gateway row), the `mercury-observatory.service` systemd
> unit running the stdlib IRC daemon (agent + bouncer listeners), the
> gateway bot with per-channel sessions, `/spawn` + `/spawnomp` rooms,
> `#parent-child` subagent trace rooms with steering, and `/exit` room
> kills — all working, no downloads, no crypto stack.
> If rooms stay dark, the unit is not running — repair with
> `mercury setup observatory` (or
> `mercury setup observatory --install-sidecar`), then check
> `systemctl --user status mercury-observatory.service`.
> Design state: `docs/design/irc-observatory.md`.

## What gets installed

The observatory is provisioned by `install.sh` (skip with
`--skip-observatory`) and is **on by default**. Provisioning is
idempotent and fail-hard — re-running it never breaks an existing
setup:

- **ircd.json** — `~/.mercury/observatory/ircd.json`, written once:
  network label, agent listener (`127.0.0.1:6669`), bouncer listener
  (`127.0.0.1:6670`), history limit. Hand-editable, never overwritten;
  delete it (or `reset` in the wizard) to re-provision from scratch.
- **Passwords** — random bouncer + agent passwords, mirrored in
  `~/.mercury/.env` (`IRC_BOUNCER_PASSWORD`, `IRC_AGENT_PASSWORD`,
  mode 0600). The daemon reads them from the environment, never from
  the command line.
- **A systemd user unit** — `mercury-observatory.service`, enabled at
  install, logs under `~/.mercury/observatory/logs/`.

Everything lives under `~/.mercury/observatory/` (config, history
database, agent tree, logs) — nothing is written into the engine tree.

## First connect with any IRC client

You connect **once**, to the bouncer, with any nick you like:
1. Get the address and password: run `mercury setup observatory` and
   read the login card (bouncer host and port are on separate lines
   because most clients want them in separate fields — never type a
   scheme like `irc://`, and never append `:port` to the hostname).
2. In your IRC client, add a server with the host and port from the
   card in their own fields, plain IRC with TLS **off**, any nickname
   you like, and the server/bouncer password from `~/.mercury/.env`
   (`IRC_BOUNCER_PASSWORD`, never printed).
3. Join `#<server>_gateway` (default `#mercury_gateway`) — the gateway
   agent lives here and chats exactly like the CLI (slash commands and
   all).

On your phone, point the client at the tailnet address from the card
(`mercury setup observatory` offers to pin the bouncer to Tailscale).
Never expose the bouncer beyond your tailnet.

## The channels

One channel per live agent:

```
#mercury_gateway          your main conversation (gateway agent)
#ace                      a /spawn agent's room (hermes, CLI parity)
#king                     a /spawnomp agent's room (omp)
#ace-cow                  a subagent's trace room (#parent-child)
```

Each subagent spawned through `delegate_task` (or the omp equivalent)
gets a `#parent-child` room named by its parent, streaming its tool
calls (`🔧`) and thinking traces (`💭`) live. Grandchild frames are
tagged `[id]`.

## Steering and commands

Write in an agent's room and it reaches that agent — mid-turn messages
steer at the next safe boundary, exactly like the CLI. Slash verbs
control:

- `/spawn <name>` — create a new hermes-side agent (indefinite
  lifespan, resumable; only `/exit` ends it).
- `/spawnomp <name>` — same, but an omp-side agent (headless).
- `/exit` — kill a spawned agent and destroy its whole room tree
  (the local transcript survives). Never runs in the gateway room.
- `/stop` — ask the agent to stop; steer it first for a wrap-up.
- `/approve` / `/deny` — reply to an approval prompt in a gateway or
  spawned-hermes room to resolve it.

Any other `/command` passes through to the agent's native command
surface — the full hermes slash registry in hermes rooms, omp's
command handling in omp rooms. `/spawn` and `/spawnomp` run only in
the gateway room.

## What disappears, and when

Channels die with their agents, never on a timer:

- **Subagent rooms** vanish when the subtree exits (the final summary
  posts to the parent's room only); a crashed destroy completes on
  the next boot from the journal instead of resurrecting the agent.
- **Spawned agents**: history survives until `/exit`, which stops the
  engine and destroys every channel in the subtree server-side.
- **Restarts are not death.** Gateway restarts, Mercury updates, and
  daemon restarts never delete channels — live agents rejoin the same
  channels on the next start (omp handles resume from their session
  files).

## The kill switch and updates

```yaml
observatory:
  enabled: true   # false: freezes the observatory — nothing is deleted
```

`observatory.enabled: false` stops all observatory activity and leaves
every channel frozen in place; re-enabling resumes. Both update
surfaces — the `/update` command and shell `mercury update` — keep the
config and unit current (there is nothing to download: the daemon is
stdlib-only). Both are config-gated: disabled means a silent skip, and
a failed refresh never blocks the Mercury update itself.

## Secondary chat (Matrix) — the other Matrix plugin

The classic single-account Matrix bot (bring-your-own homeserver and
access token) still ships, labeled **Secondary chat (Matrix)** so it
can't be confused with the observatory. The two never interact: the
observatory is its own IRC network; Secondary chat is your personal
account on some other server.
