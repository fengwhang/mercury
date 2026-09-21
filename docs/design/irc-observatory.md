# IRC Observatory — design (replaces the Matrix observatory, retired 2026-09)

The Matrix/tuwunel/sidecar stack is gone (broken beyond repair: E2EE
wedges, cross-process handle handoffs, orphan rooms). This doc is the
truth doc for its stdlib IRC replacement.

## 0. What this is

A first-class IRC UI for Mercury, bundled with the distribution: a
local IRC network that mirrors every live agent session on the machine
as channels, streams tool calls (and omp-side reasoning) into
per-agent rooms, and lets the owner steer, stop, and converse with any
agent — gateway, spawned orchestrator, or subagent, all depths. Chatting
in a room feels exactly like the CLI: same sessions, same slash
commands, same mid-turn steering.

## 1. Decisions

| # | Decision |
|---|----------|
| D1 | Bundled, default ON at install; `observatory.enabled: false` freezes (never deletes). |
| D2 | Ships its own network: stdlib asyncio ircd, no downloads, no message crypto. Agent listener (`127.0.0.1:6669`, or the tailnet IP on request) + plaintext bouncer (`127.0.0.1:6670`) + TLS bouncer (`127.0.0.1:6697`) on the same channel state. The daemon replays the last N messages per channel on JOIN (SQLite-persisted, survives restarts). TLS serves strict clients only (self-signed CA in `observatory/tls/`); the tailnet stays the perimeter. Frontend is The Lounge (one per user, npm-installed, points at every mercury ircd as a network); direct IRC clients may use the bouncer listeners. |
| D3 | The gateway owns ALL IRC I/O in-process (rooms + spawn + feed producers). No sidecar, no cross-process handle handoff — the bug class that killed Matrix cannot recur. |
| D4 | One channel per agent. Gateway → `#<server>_gateway`; `/spawn` + `/spawnomp <name>` → `#<name>`; delegate children → `#<parent>-<child>` (parent names the child). IRC channels are created on first JOIN and destroyed server-side (OPER `DESTROY`) on `/exit`. |
| D5 | Full steering: chat with any delegate child AND any omp in-process subagent, all depths. Tool calls render as `🔧` lines, thinking as `💭` lines, grandchild frames tagged `[id]`. |
| D6 | Agent `name` is required at both spawn surfaces. Unicode allowed in display/channel topics; channel slugs are lowercase (`clean_channel`). |
| D7 | Single owner; the bouncer password is the auth (any nick). No per-user ACL in v1 — the tailnet is the perimeter. |
| D8 | Annihilation semantics: `/exit` dead-marks the whole subtree + journals channels atomically, stops engines, destroys channels, deletes rows. Crashed destroys replay from the journal on next boot (dead-or-deleted in every observable state). Restart is not death: live 0-agents rejoin + resume (omp via session files). |
| D9 | `/spawn <name>` = hermes-side agent: a plain gateway session keyed by channel (first message starts it — slash commands, approvals, mid-turn steering free). `/spawnomp <name>` = headless omp RPC child pumped by `rooms.handle_omp_message` (idle → task, busy → steer). No cap on live orchestrators. |
| D10 | Child rooms steer the live child: omp children via `transport.steer` (registered by the feed watcher; one-shots are read-only traces); spawned-omp rooms via `run_task`/`steer`. Mid-turn text lands at the next safe boundary — the in-flight tool call is never cut. |
| D11 | Tailscale pins: the bouncer listener pins to the tailnet IP on request, and the agent listener offers localhost-vs-tailnet at setup (whole fleet reachable when pinned). The Lounge binds localhost or tailnet by choice. `mercury setup observatory` offers each when the tailnet is up. |
| D12 | Gateway wiring is explicit: `mercury setup observatory` offers to write the gateway's `IRC_*` env (server/port/nick/channel/password) so the gateway bot joins the network as `<server>_gateway`. |

## 2. Architecture

1. **ircd** (`observatory/ircd.py`) — stdlib asyncio server. Two
   listeners, one channel state, SQLite history. `OPER` (agent
   password) + `DESTROY #chan` (oper-only room kill for `/exit`).
   Systemd user unit `mercury-observatory.service`.
2. **rooms** (`observatory/rooms.py`) — naming, frame formatting, the
   `RoomManager` (state.db tree: `room_id` = channel, `mxid` = nick),
   the `BotSink` transport (the gateway IRC adapter registers itself),
   the sync producer queue + async pump (frames wait while the bot is
   down), child/omp steer registries, `boot_resync` support.
3. **spawn** (`observatory/spawn.py`) — engine side of
   `/spawn`/`/spawnomp`/`/exit`: depth-0 rows, omp RPC children,
   channel journal with atomic dead-mark + replay.
4. **Gateway integration** — the IRC platform adapter
   (`plugins/platforms/irc/adapter.py`) multiplexes channels:
   per-channel sessions, no nick-addressing in managed rooms, dynamic
   join/part/destroy, child/omp room routing into the steer paths.
   `/spawn` + `/spawnomp` + `/exit` are gateway slash commands scoped
   by channel. The delegate-child feed watcher
   (`gateway_session.py`) streams lifecycle + OmpFeed frames into the
   rooms queue and registers room steerers.
5. **provision** (`observatory/provision.py`) — idempotent:
   `ircd.json` → passwords (`.env` mirror) → gateway row → unit.
   Entry points: install.sh, `mercury setup observatory`,
   `python -m observatory.provision`, `mercury update` tail
   (`provision_if_missing` + `refresh_for_update`).

## 3. Fresh-eyes notes (why not Matrix)

E2EE on a self-hosted single-user network bought nothing (the tailnet
is the perimeter) and cost everything: per-device Olm machines,
one-time-key pools, Megolm sessions, cross-signing trust, and a second
daemon whose handle handoff never worked. IRC gives presence, history,
and rooms with ~700 lines of stdlib. TLS termination exists on the ircd
(a 6697 bouncer with a provisioned CA) for strict clients —
not message-layer crypto.
