# IRC response recovery follow-up (0.2.16)

0.2.15 did not restore the reported Pi workflow. Its fleet check proved only
nickname presence. This follow-up reproduces a stronger failure against the
0.2.15 source and checks complete socket → dispatch → reply round trips.

## The gateway evicts itself during resync

`observatory.platform_hook.boot_resync()` called `ensure_identity()` for every
live row, including `gw`. The gateway row's `mxid` is the very nickname that
`IRCAdapter` uses for its receive/dispatch connection. Opening a send-only
`IdentityConn` under that nickname makes this fork's newest-nick-wins reclaim
logic evict the gateway receiver. Every room relies on that single receiver.

The expected nickname still appears in NAMES, so fleet presence can report
success while no agent receives new turns. The round-trip regression test
gets a successful response before resync, then fails on 0.2.15 because the
server's gateway connection has changed to the send-only clone.

This duplicate-identity bug exists in the 0.2.4 resync loop too. The 0.2.5
changes made resync discover state even before the boot sidecar finished,
exposing it on startup paths that previously skipped resync. This is a
code-level explanation, not a claim that the Pi's exact timing was observed.

Fixes:

- Exclude the gateway row from send-only identity creation.
- Defence in depth: `ensure_identity()` reuses the gateway adapter whenever
  the requested nickname is its configured/current nickname.
- Use the agent-listener password for agent identities, matching IRCAdapter;
  keep the legacy server-password fallback for single-secret installs.
- Refuse an unauthenticated nickname reclaim. Wrong listener credentials
  must not evict an authenticated connection.
- Scope server-side channel cleanup to the current nick owner. An old
  socket's delayed QUIT must not remove a replacement from its rooms.

## Verification is no longer presence-only

Doctor and restart/fleet verification send a private nonce challenge to the
managed observatory gateway. Only the receive adapter answers, via NOTICE;
no model turn, room transcript, tool call, or credential value is involved.
A send-only identity can be present but cannot pass this check. Public IRC
and ordinary CTCP messages keep their previous behavior.

Fleet verification now fails on resync-reported errors and explicitly labels
presence separately from transport. It still does not claim provider health.
The resync timestamp is compared with the start of restart, not a later
verification timestamp. NAMES parsing also includes the first listed nick.

## ThinkPad cross-machine disconnection

The previous broad test run executed real `systemctl --user stop` calls from
reset tests using temporary Mercury data. Those unqualified unit names still
targeted the operator's live IRC daemon and Lounge. This was a test-isolation
error, not evidence of a new wire-protocol incompatibility.

The existing ThinkPad daemon, Lounge, and gateway services were restarted.
The agent, plain-IRC, TLS-IRC, and Lounge listener ports were checked open,
and the existing gateway nick was confirmed back in its room. No credentials
or saved network configuration were reset; the stable installation remains
0.2.4. A remote Lounge may need its saved network reconnected.

Observatory tests now sandbox HOME (including unit-file paths) and stub
systemctl by default. Service-specific tests can explicitly override the
stub. The live services stayed active through the subsequent broad test run.

## Validation

- 42 focused transport, identity, teardown, doctor, and restart tests passed.
- New real-daemon tests verify replies in gateway, Hermes, and OMP rooms,
  before/after resync and after repeated resync, with both shared and split
  passwords. Engine handlers are deterministic test doubles, not paid model
  calls; real IRC sockets, adapter dispatch, room routing, and sends are used.
- The Lounge fork's actual draft/multiline wire framing preserves markdown
  and LaTeX text as one inbound turn and a multiline reply.
- Separate tests reject send-only fake gateway health, unauthenticated nick
  takeover, and delayed-old-socket removal of new channel membership.
- Broad observatory + restart suite: 270 passed, the same six previously
  documented baseline failures (three depend on unavailable TLS tooling).

The 0.2.15 EOF/PING fixes, trace plaintext rendering, reply markdown/KaTeX,
multiline framing, and credential protections are retained. Pi/provider and
cross-machine UI validation remain for the actual installation.
