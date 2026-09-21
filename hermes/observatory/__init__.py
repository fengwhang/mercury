"""IRC Observatory — Mercury's bundled local chat network.

One stdlib asyncio IRC daemon (``ircd``: agent listener + bouncer
listener on the same channel state, SQLite history replay), one
channel per live agent, driven in-process by the gateway
(``rooms`` + ``spawn`` + ``gateway_session`` feed producers).

Modules:
  ircd — the daemon (two listeners, OPER/DESTROY, history persist).
  rooms — channel naming, frame formatting, room manager, producer
      queue, child/omp inbound steering.
  spawn — /spawn + /spawnomp + /exit lifecycle over channels.
  state — agent-tree SQLite store ($MERCURY_HOME/observatory/state.db,
      WAL): room_id is the IRC channel, mxid the agent nick.
  config_gen — pure generators for ircd.json + the systemd user unit.
  provision — idempotent orchestrator: config, passwords, unit install.
      Entry point for install.sh and `mercury setup observatory`.
  gateway_session — gateway-turn runner + child feed producers (the
      queue side; the room side lives in rooms.py).
  gateway_transport — control-socket transport (engine-agnostic).
  platform_hook — gateway boot seam (LAST_BOOT, try_boot, open_state).
"""
from observatory.config_gen import ObservatoryPaths
from observatory.state import SCHEMA_VERSION, ObservatoryState, purge_on_death

__all__ = [
    "ObservatoryPaths",
    "ObservatoryState",
    "SCHEMA_VERSION",
    "purge_on_death",
]
