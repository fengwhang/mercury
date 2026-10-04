"""Shared-metrics efficiency observations (parity port of stock ``hermes_cli/observability/shared_metrics_efficiency.py``).

Minimal closure for the ported ``turn_*`` modules: the request-tool observation entry point.
Stock's ``record_session_tools`` runtime pipeline (``_Runtime.efficiency`` session model) is
not present in Mercury's consolidated relay; until it lands, the entry point degrades to a
no-op exactly as its own best-effort error handling specifies (telemetry never raises on the
agent thread).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---- agent-side entry points (agent thread; never raise) ---------------------------------------

def _internal_agent(agent: Any) -> bool:
    """Hermes-owned forks (background review, curator) share the user's session id."""
    return getattr(agent, "_memory_write_origin", None) == "background_review"


def _enabled() -> bool:
    from .relay_shared_metrics import enabled

    return enabled()


def observe_request_tools(agent: Any, tools_for_api: Any) -> None:
    """The tool definitions one primary request carries (evidence for trimming default toolsets)."""
    try:
        if getattr(agent, "_persist_disabled", False) or _internal_agent(agent) or not _enabled():
            return
        from .relay_shared_metrics import record_session_tools

        record_session_tools(str(getattr(agent, "session_id", "") or ""), agent, tools_for_api or [])
    except Exception:
        logger.debug("Shared-metrics tool overhead not observed", exc_info=True)
