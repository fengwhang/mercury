# IRC response recovery candidate (0.2.15)

Code comparison: v0.2.4 → v0.2.13, with the existing v0.2.14 fixes retained.

## Confirmed regression

In `hermes/plugins/platforms/irc/adapter.py`, commit `82e1c367`
(v0.2.12) removed the receive loop's `finally:` and moved connection-loss
handling into `except Exception`. Normal remote EOF is not an exception.
Consequently, EOF leaves the handler queue running, the writer and bot sink
stale, and no retryable failure notification. The gateway can look online
while no longer receiving user messages. v0.2.14 restored `finally`; this
candidate retains that repair and adds a regression test that fails against
v0.2.13. It also guards the entire cleanup by connection generation: protecting
only the writer (as v0.2.14 did) still lets an old receiver stop the new handler
and report a new, healthy connection as lost.

## Additional confirmed silent-output failure

`hermes/observatory/identity.py` is unchanged between v0.2.4 and v0.2.13,
so this is not itself a newly introduced regression. However, the expanded
boot/resurrection behavior relies on these long-lived per-room connections.
They ignored server PINGs, and `_drain()` treated EOF as an ordinary end of
buffered input. A successful local socket drain could then return True for
an undelivered reply, suppressing the gateway's fallback sender.

The candidate adds a dedicated receive task for each identity, answers PINGs,
invalidates the writer on EOF, checks EOF before sends, bounds socket drains,
and retains the existing one-reconnect retry and multiline framing. Receiver
cleanup is writer-specific, so an old receiver cannot clear a replacement.

## Validation

- New identity tests fail against v0.2.13 for missing PONG and false-positive
  send success at EOF. The actual remote-drop/multiline test exercises the
  real Mercury daemon and a receiving peer.
- New gateway EOF regression test fails against v0.2.13.
- Focused identity, adapter teardown, and room tests: 34 passed.
- Full observatory run before the final adapter test additions: 254 passed,
  6 failed. An untouched v0.2.14 worktree reproduces the same six failures
  (251 passed): lobby-resync expectation, server auto-join expectation,
  upload-refusal expectation, and three unavailable TLS certificate tests.
- Rendering and credential guards are not removed or relaxed. The existing
  trace-prefix plaintext path and user-message markdown/KaTeX path remain.

This establishes transport defects and regression coverage, not proof that
all reported Raspberry Pi symptoms have one cause. Test this candidate on
that machine, particularly after idle periods and daemon/gateway restarts.
