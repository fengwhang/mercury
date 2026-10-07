"""Gateway health and diagnostics signal producer.

This module keeps the plane narrow: service health monitoring plus
redacted operational diagnostics. It reuses the existing gateway runtime-status
contract and emits content-free metrics/events. No prompts, messages, tool args,
session history, audit records, or product analytics belong here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.monitoring.events import GatewayDiagnosticEvent, GatewayHealthEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GatewayMetric:
    name: str
    value: int | float
    attributes: Dict[str, str]


@dataclass(frozen=True, slots=True)
class GatewayHealthSnapshot:
    metrics: List[GatewayMetric]
    events: List[GatewayHealthEvent | GatewayDiagnosticEvent]


_RUNNING_PLATFORM_STATES = {"running", "connected", "ok", "ready"}
_FATAL_PLATFORM_STATES = {"fatal", "degraded", "error", "failed"}
_KNOWN_GATEWAY_STATES = {
    "starting", "draining", "stopping", "stopped", "startup_failed", "unknown"
} | _RUNNING_PLATFORM_STATES | _FATAL_PLATFORM_STATES
_KNOWN_PLATFORM_STATES = _RUNNING_PLATFORM_STATES | _FATAL_PLATFORM_STATES | {
    "connecting", "disconnected", "disabled", "paused", "retrying", "unknown"
}
_SUPERVISION_MODES = {"systemd", "s6", "container", "launchd", "manual", "unknown"}
_SOURCE_LOGGER_RE = re.compile(r"^gateway(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _allowed_logger(name: str) -> bool:
    return name == "gateway" or name.startswith("gateway.")


def source_logger_for_diagnostics(name: Any) -> Optional[str]:
    """Return a bounded source-controlled gateway logger name."""
    value = str(name or "")
    return value if len(value) <= 128 and _SOURCE_LOGGER_RE.fullmatch(value) else None


def redact_gateway_message(message: Any) -> str:
    """Redact gateway diagnostic free text for local reports.

    Single scrub path: everything goes through
    ``agent.monitoring.redaction.redact_for_export`` (unconditional
    secrets + PII), then is length-bounded.
    """
    try:
        from agent.monitoring.redaction import redact_for_export
        redacted = redact_for_export(str(message or "")) or ""
    except Exception:
        redacted = "[redaction-unavailable]"
    return redacted[:500]


def classify_gateway_error(raw: Any) -> str:
    s = str(raw or "").lower()
    if any(k in s for k in ("auth", "token", "unauthorized", "forbidden", "401", "403")):
        return "auth_failed"
    if "rate" in s and "limit" in s:
        return "rate_limited"
    if "timeout" in s or "timed out" in s:
        return "timeout"
    if any(
        k in s
        for k in (
            "network",
            "connection",
            "dns",
            "socket",
            "connect call failed",
            "failed to connect",
            "cannot connect",
            "unreachable",
            "name resolution",
        )
    ):
        return "network_error"
    if any(k in s for k in ("config", "missing", "invalid")):
        return "invalid_config"
    if "startup" in s:
        return "startup_failed"
    if "fatal" in s:
        return "platform_fatal"
    return "unknown"


def classify_exit_reason(
    raw: Any, *, state: Any, restart_requested: bool
) -> Optional[str]:
    """Reduce free-form shutdown text to a bounded operational class."""
    if restart_requested:
        return "restart_requested"
    state_name = str(state or "").lower()
    if raw is None and state_name != "startup_failed":
        return None
    classified = classify_gateway_error(raw)
    if state_name == "startup_failed":
        return classified if classified != "unknown" else "startup_failed"
    text = str(raw or "").lower()
    if "signal" in text or "sigterm" in text or "sigint" in text:
        return "signal"
    if state_name == "stopped" and any(word in text for word in ("shutdown", "stop")):
        return "planned_stop"
    return classified


def _bounded_state(raw: Any, *, allowed: set[str]) -> str:
    state = str(raw or "unknown").lower()
    return state if state in allowed else "unknown"


def _safe_metric_value(raw: Any, *, limit: int = 128) -> str:
    try:
        from agent.monitoring.redaction import redact_for_export
        value = redact_for_export(str(raw or "")) or "unknown"
    except Exception:
        return "unknown"
    return value[:limit]


def _safe_instance_id(raw: Any) -> str:
    """Return a stable opaque instance key without exporting the source ID."""
    value = str(raw or "unknown").encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(value).hexdigest()[:24]}"


def subsystem_for_logger(logger_name: str) -> str:
    if logger_name == "gateway.relay" or logger_name.startswith("gateway.relay."):
        return "platform.relay"
    if logger_name.startswith("gateway.platforms."):
        parts = logger_name.split(".")
        if len(parts) >= 3 and parts[2]:
            return f"platform.{parts[2]}"
    if logger_name.startswith("gateway.platforms"):
        return "platform"
    if logger_name.startswith("gateway"):
        return "gateway"
    return "gateway"


def platform_for_subsystem(subsystem: str) -> Optional[str]:
    if subsystem.startswith("platform."):
        return subsystem.split(".", 1)[1] or None
    return None


def _parse_active_agents(raw: Any) -> int:
    try:
        from gateway.status import parse_active_agents
        return parse_active_agents(raw)
    except Exception:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0


def _derive_busy(gateway_running: bool, gateway_state: Any, active_agents: Any) -> bool:
    try:
        from gateway.status import derive_gateway_busy
        return derive_gateway_busy(
            gateway_running=gateway_running,
            gateway_state=gateway_state,
            active_agents=active_agents,
        )
    except Exception:
        return bool(gateway_running and gateway_state == "running" and _parse_active_agents(active_agents) > 0)


def _derive_drainable(gateway_running: bool, gateway_state: Any) -> bool:
    try:
        from gateway.status import derive_gateway_drainable
        return derive_gateway_drainable(gateway_running=gateway_running, gateway_state=gateway_state)
    except Exception:
        return bool(gateway_running and gateway_state == "running")


def _base_attrs(*, profile: str, install_id: str, version: str, supervision_mode: str) -> Dict[str, str]:
    mode = str(supervision_mode or "unknown").lower()
    return {
        "service.instance.id": _safe_instance_id(install_id),
        "service.version": _safe_metric_value(version, limit=64),
        "mercury.supervision_mode": mode if mode in _SUPERVISION_MODES else "unknown",
    }


def _metric(name: str, value: int | float, attrs: Dict[str, str], **extra: str) -> GatewayMetric:
    out = dict(attrs)
    for key, val in extra.items():
        if val is not None:
            out[key] = _safe_metric_value(val)
    return GatewayMetric(name=name, value=value, attributes=out)


def build_gateway_health_snapshot(
    runtime: Optional[dict[str, Any]],
    *,
    gateway_running: bool,
    profile: str,
    install_id: str,
    version: str,
    supervision_mode: str = "unknown",
) -> GatewayHealthSnapshot:
    """Convert gateway_state.json-compatible runtime state into P0 signals."""
    runtime = runtime or {}
    gateway_state = _bounded_state(
        runtime.get("gateway_state"), allowed=_KNOWN_GATEWAY_STATES
    )
    active_agents = _parse_active_agents(runtime.get("active_agents", 0))
    busy = _derive_busy(gateway_running, gateway_state, active_agents)
    drainable = _derive_drainable(gateway_running, gateway_state)
    platforms = runtime.get("platforms") if isinstance(runtime.get("platforms"), dict) else {}
    base = _base_attrs(profile=profile, install_id=install_id, version=version, supervision_mode=supervision_mode)

    metrics: list[GatewayMetric] = [
        _metric("mercury.gateway.up", 1 if gateway_running else 0, base),
        _metric("mercury.gateway.active_agents", active_agents, base),
        _metric("mercury.gateway.busy", 1 if busy else 0, base),
        _metric("mercury.gateway.drainable", 1 if drainable else 0, base),
        _metric("mercury.gateway.restart_requested", 1 if runtime.get("restart_requested") else 0, base),
    ]
    if gateway_state:
        metrics.append(_metric("mercury.gateway.state", 1, base, **{"mercury.gateway.state": str(gateway_state)}))

    fatal_count = 0
    events: list[GatewayHealthEvent | GatewayDiagnosticEvent] = []
    for platform, pdata in platforms.items():
        pdata = pdata if isinstance(pdata, dict) else {}
        state = _bounded_state(
            pdata.get("state"), allowed=_KNOWN_PLATFORM_STATES
        )
        raw_error = pdata.get("error_code") or pdata.get("error_message")
        error_code = classify_gateway_error(raw_error)
        is_up = state in _RUNNING_PLATFORM_STATES
        is_degraded = state in _FATAL_PLATFORM_STATES
        if is_degraded:
            fatal_count += 1
        metrics.append(_metric(
            "mercury.platform.up",
            1 if is_up else 0,
            base,
            **{"mercury.platform": str(platform), "mercury.platform.state": state},
        ))
        metrics.append(_metric(
            "mercury.platform.degraded",
            1 if is_degraded else 0,
            base,
            **{"mercury.platform": str(platform), "mercury.platform.state": state, "mercury.error_code": error_code},
        ))
        if is_degraded:
            events.append(GatewayDiagnosticEvent(
                name="platform.fatal",
                subsystem=f"platform.{platform}",
                platform=str(platform),
                error_code=error_code,
                error_class=classify_gateway_error(error_code or pdata.get("error_message")),
                profile=profile,
                version=version,
                severity="error" if state == "fatal" else "warning",
            ))

    events.insert(0, GatewayHealthEvent(
        name="gateway.health_snapshot",
        gateway_state=str(gateway_state) if gateway_state is not None else None,
        active_agents=active_agents,
        gateway_busy=busy,
        gateway_drainable=drainable,
        platform_count=len(platforms),
        fatal_platform_count=fatal_count,
        profile=profile,
        install_id=install_id,
        version=version,
        supervision_mode=supervision_mode,
        pid=_coerce_pid(runtime.get("pid")),
    ))
    return GatewayHealthSnapshot(metrics=metrics, events=events)


def _safe_profile() -> str:
    try:
        from mercury_cli.profiles import get_active_profile_name
        return str(get_active_profile_name() or "default")
    except Exception:
        return "default"


def _safe_version() -> str:
    try:
        from mercury_cli import __version__
        return str(__version__)
    except Exception:
        return "unknown"


def emit_runtime_status_transition(previous: Optional[dict[str, Any]], current: dict[str, Any]) -> None:
    """Emit immediate content-free gateway events for runtime status changes.

    Called by gateway.status.write_runtime_status after persisting the new status.
    Fully fail-open: failures never affect gateway status writes.
    """
    try:
        from agent.monitoring import emitter
        out: list[GatewayHealthEvent | GatewayDiagnosticEvent] = []
        profile = _safe_profile()
        version = _safe_version()
        old_gateway_state = _bounded_state(
            (previous or {}).get("gateway_state"), allowed=_KNOWN_GATEWAY_STATES
        ) if (previous or {}).get("gateway_state") is not None else None
        new_gateway_state = _bounded_state(
            current.get("gateway_state"), allowed=_KNOWN_GATEWAY_STATES
        ) if current.get("gateway_state") is not None else None
        if old_gateway_state != new_gateway_state and new_gateway_state:
            out.append(GatewayHealthEvent(
                name="gateway.lifecycle",
                gateway_state=new_gateway_state,
                old_state=old_gateway_state,
                new_state=new_gateway_state,
                exit_reason=classify_exit_reason(
                    current.get("exit_reason"),
                    state=new_gateway_state,
                    restart_requested=bool(current.get("restart_requested")),
                ),
                restart_requested=bool(current.get("restart_requested")),
                active_agents=_parse_active_agents(current.get("active_agents", 0)),
                profile=profile,
                version=version,
                pid=_coerce_pid(current.get("pid")),
            ))
            if new_gateway_state == "startup_failed":
                out.append(GatewayDiagnosticEvent(
                    name="gateway.startup_failed",
                    subsystem="gateway",
                    error_class=classify_gateway_error(current.get("exit_reason") or "startup_failed"),
                    error_code=classify_gateway_error(current.get("exit_reason") or "startup_failed"),
                    profile=profile,
                    version=version,
                    severity="error",
                ))
            if new_gateway_state == "stopped":
                out.append(GatewayHealthEvent(
                    name="gateway.exit",
                    gateway_state=new_gateway_state,
                    old_state=old_gateway_state,
                    new_state=new_gateway_state,
                    exit_reason=classify_exit_reason(
                        current.get("exit_reason"),
                        state=new_gateway_state,
                        restart_requested=bool(current.get("restart_requested")),
                    ),
                    restart_requested=bool(current.get("restart_requested")),
                    active_agents=_parse_active_agents(current.get("active_agents", 0)),
                    profile=profile,
                    version=version,
                    pid=_coerce_pid(current.get("pid")),
                ))

        old_platforms_raw = (previous or {}).get("platforms")
        new_platforms_raw = current.get("platforms")
        old_platforms = old_platforms_raw if isinstance(old_platforms_raw, dict) else {}
        new_platforms = new_platforms_raw if isinstance(new_platforms_raw, dict) else {}
        for platform, pdata in new_platforms.items():
            pdata = pdata if isinstance(pdata, dict) else {}
            prev_raw = old_platforms.get(platform, {})
            prev = prev_raw if isinstance(prev_raw, dict) else {}
            old_state = _bounded_state(
                prev.get("state"), allowed=_KNOWN_PLATFORM_STATES
            ) if prev.get("state") is not None else None
            new_state = _bounded_state(
                pdata.get("state"), allowed=_KNOWN_PLATFORM_STATES
            ) if pdata.get("state") is not None else None
            if old_state == new_state or not new_state:
                continue
            error_code = classify_gateway_error(pdata.get("error_code") or pdata.get("error_message"))
            severity = "error" if new_state.lower() in {"fatal", "failed", "error"} else "warning"
            out.append(GatewayDiagnosticEvent(
                name="platform.state_change",
                subsystem=f"platform.{platform}",
                platform=str(platform),
                old_state=old_state,
                new_state=new_state,
                error_code=error_code,
                error_class=error_code,
                profile=profile,
                version=version,
                severity=severity,
            ))
            if new_state.lower() in _FATAL_PLATFORM_STATES:
                out.append(GatewayDiagnosticEvent(
                    name="platform.fatal",
                    subsystem=f"platform.{platform}",
                    platform=str(platform),
                    error_code=error_code,
                    error_class=error_code,
                    profile=profile,
                    version=version,
                    severity=severity,
                ))
        for ev in out:
            emitter.emit(ev)
    except Exception:
        logging.getLogger(__name__).debug("gateway runtime status transition emit failed", exc_info=True)


def _coerce_pid(raw: Any) -> Optional[int]:
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


class GatewayDiagnosticLogHandler(logging.Handler):
    """Allowlisted warning/error bridge for gateway-owned diagnostics."""

    def __init__(self, *, profile: str = "default", version: str = "unknown") -> None:
        super().__init__(level=logging.WARNING)
        self.profile = profile
        self.version = version

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.WARNING:
                return
            if not _allowed_logger(record.name):
                return
            subsystem = subsystem_for_logger(record.name)
            message = record.getMessage()
            error_class = classify_gateway_error(message)
            event = GatewayDiagnosticEvent(
                name=f"gateway.log.{record.levelname.lower()}",
                subsystem=subsystem,
                source_logger=source_logger_for_diagnostics(record.name),
                platform=platform_for_subsystem(subsystem),
                error_class=error_class,
                error_code=error_class,
                profile=self.profile,
                version=self.version,
                severity=record.levelname.lower(),
            )
            from agent.monitoring import emitter
            emitter.get_emitter().emit(event)
        except Exception:
            logging.getLogger(__name__).debug("gateway diagnostic emit failed", exc_info=True)


def _supervision_mode() -> str:
    if os.environ.get("INVOCATION_ID"):
        return "systemd"
    if os.environ.get("S6_CMD_ARG0") or os.environ.get("S6_VERSION"):
        return "s6"
    if os.environ.get("container") or os.path.exists("/.dockerenv"):
        return "container"
    if os.environ.get("LAUNCHD_SOCKET"):
        return "launchd"
    return "manual"


def _read_gateway_snapshot(config: Dict[str, Any]):
    try:
        from gateway.status import read_runtime_status, runtime_status_pid_is_live
        runtime = read_runtime_status() or {}
        gateway_running = runtime_status_pid_is_live(runtime)
    except Exception:
        runtime = {}
        gateway_running = False
    return build_gateway_health_snapshot(
        runtime,
        gateway_running=gateway_running,
        profile=_safe_profile(),
        install_id=str((config.get("monitoring") or {}).get("install_id") or "unknown"),
        version=_safe_version(),
        supervision_mode=_supervision_mode(),
    )


def _read_cron_snapshot():
    from agent.monitoring.cron_health import build_cron_health_snapshot

    return build_cron_health_snapshot()


def _read_background_work_count() -> int:
    """Count live background/subagent work that ``active_agents`` does NOT include.

    ``mercury.gateway.active_agents`` counts foreground turns + in-flight cron
    jobs + API runs, but deliberately excludes backgrounded ``delegate_task``
    subagents, ``terminal(background=true)`` processes, kanban workers, and the
    runner's own background tasks (they are tracked only for the scale-to-zero
    suspend guard, ``_scale_to_zero_has_live_background_work``). Without this
    metric a peer churning through delegated subagents shows ``active_agents=0``
    on the fleet dashboard. Best-effort and content-free: a single integer,
    no job/task identity. Returns 0 if a source can't be imported.

    Delegation is counted TASK-granular (``active_task_count``): a fan-out batch
    of N subagents contributes N, not 1, so the metric reflects real concurrent
    subagent load rather than dispatch-unit/pool-slot count. This intentionally
    differs from the async pool's capacity accounting (one batch = one slot).
    """
    total = 0
    try:
        from tools.async_delegation import active_task_count

        total += max(0, int(active_task_count()))
    except Exception:
        logger.debug("background-work async-delegation count failed", exc_info=True)
    try:
        from tools.process_registry import process_registry

        total += max(0, int(process_registry.count_running()))
    except Exception:
        logger.debug("background-work process-registry count failed", exc_info=True)
    return total


def _read_background_delegations_count() -> int:
    """Count live async delegation UNITS (dispatch/pool slots).

    Complements ``_read_background_work_count`` (which is task-granular): this
    counts each ``delegate_task`` dispatch as ONE regardless of fan-out width,
    matching the async pool's capacity accounting (a batch = one slot). Together
    the two metrics let an operator see both slot pressure
    (``background_delegations``, alert vs ``max_concurrent_children``) and real
    concurrent subagent load (``background_work``). Delegations only — it does
    not include ``terminal(background)`` / kanban work, which are already folded
    into ``background_work``. Best-effort; 0 if the source can't be imported.
    """
    try:
        from tools.async_delegation import active_count

        return max(0, int(active_count()))
    except Exception:
        logger.debug("background-delegations count failed", exc_info=True)
        return 0


def read_runtime_health_snapshot(config: Dict[str, Any]) -> GatewayHealthSnapshot:
    """Read local gateway, cron, and background-work health without exporters."""
    gateway_snapshot = _read_gateway_snapshot(config)
    # Background/subagent work — a distinct metric from active_agents (which
    # never counts it). Appended to the gateway snapshot so it rides the same
    # base resource attributes (service.instance.id etc.).
    try:
        base = dict(gateway_snapshot.metrics[0].attributes) if gateway_snapshot.metrics else {}
        gateway_snapshot.metrics.append(
            GatewayMetric(
                name="mercury.gateway.background_work",
                value=_read_background_work_count(),
                attributes=base,
            )
        )
        gateway_snapshot.metrics.append(
            GatewayMetric(
                name="mercury.gateway.background_delegations",
                value=_read_background_delegations_count(),
                attributes=base,
            )
        )
    except Exception as exc:
        logger.warning(
            "background-work snapshot unavailable (error_type=%s)",
            type(exc).__name__,
        )
        logger.debug("background-work snapshot traceback", exc_info=True)
    try:
        cron_snapshot = _read_cron_snapshot()
    except Exception as exc:
        # Content-free visibility: cron telemetry silently dropping out is a
        # release-relevant regression, so surface it at WARNING with only the
        # exception *type* name (never the message, which could carry paths or
        # other environment detail). exc_info stays on the DEBUG record.
        logger.warning(
            "cron health snapshot unavailable (error_type=%s)",
            type(exc).__name__,
        )
        logger.debug("cron health snapshot traceback", exc_info=True)
        return gateway_snapshot
    gateway_snapshot.metrics.extend(cron_snapshot.metrics)
    return gateway_snapshot


__all__ = [
    "GatewayMetric",
    "GatewayHealthSnapshot",
    "GatewayDiagnosticLogHandler",
    "build_gateway_health_snapshot",
    "read_runtime_health_snapshot",
    "classify_gateway_error",
    "source_logger_for_diagnostics",
    "redact_gateway_message",
]
