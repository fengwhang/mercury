"""Matrix Observatory — Mercury's bundled local Matrix stack.

Spec: docs/design/matrix-observatory.md. Phase 2 = Tuwunel bundling
(fetch-at-install per D16). Phase 3 = the appservice sidecar (this
package); M3a ships the skeleton modules below — no live homeserver
needed to import or test any of them.

Modules:
  config_gen — pure generators for tuwunel.toml, the appservice
      registration YAML and the systemd user unit (no I/O; unit-tested).
  tuwunel — GitHub release query (>= 1.8.1 hard gate), download and
      install/refresh of the static binary (fetch boundary injectable).
  provision — idempotent fail-hard orchestrator: config, appservice
      registration, owner bootstrap, systemd unit. Entry point for
      install.sh, the future first-gateway-start hook, and `mercury update`.
  state — agent-tree SQLite store ($MERCURY_HOME/observatory/state.db,
      WAL) with D8 depth semantics and D17 live-only slug collisions.
  identity — display-name (unicode-preserving) + MXID slug policy
      (strict lowercase ASCII, ``merc_`` prefix, live-only suffixing).
  tree — pure forest assembly + desired space hierarchy + diff plan
      (create/attach/detach/room-add) against a matrix snapshot.
  appservice — aiohttp transaction-intake skeleton (dedup by txnId,
      token middleware, event queue). Imported lazily: aiohttp is an
      optional extra and config_gen/provision must stay importable
      without it.
"""
from observatory.config_gen import ObservatoryPaths
from observatory.identity import (
    assign_slug,
    qualified_display_name,
    sanitize_display_name,
    slugify,
    virtual_mxid,
)
from observatory.state import SCHEMA_VERSION, ObservatoryState, purge_on_death
from observatory.tree import (
    build_forest,
    desired_plan,
    diff_plan,
)

__all__ = [
    "APPSERVICE_PORT_DEFAULT",
    "ObservatoryPaths",
    "ObservatoryState",
    "SCHEMA_VERSION",
    "TransactionIntake",
    "assign_slug",
    "build_forest",
    "desired_plan",
    "diff_plan",
    "make_app",
    "purge_on_death",
    "qualified_display_name",
    "sanitize_display_name",
    "slugify",
    "virtual_mxid",
]


def __getattr__(name: str):  # PEP 562 — aiohttp is optional
    if name in ("TransactionIntake", "make_app"):
        from observatory import appservice

        return getattr(appservice, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
