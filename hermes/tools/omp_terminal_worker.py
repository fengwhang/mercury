"""OMP shell transport using Hermes' existing environment backends.

OMP enforces its native approval policy before sending a command here. Do not
run Hermes' approval gate again or replace OMP's policy with Hermes' settings.
The process belongs to one OMP shell session; descendant processes start their
own worker and inherit the same Mercury profile/configuration environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(backend: str) -> None:
    from mercury_cli.config import apply_terminal_config_to_env
    from tools.terminal_tool import (
        _get_env_config, _create_environment, _ssh_config_from_config,
        _container_config_from_config, _is_container_backend,
    )
    from tools.interrupt import set_interrupt

    # Resolve current explicit config rather than a gateway's stale env export.
    apply_terminal_config_to_env()
    config = _get_env_config()
    if config["env_type"] != backend or backend == "local":
        raise RuntimeError("Terminal configuration changed; retry with the current backend")

    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    environment = None
    output_lock = threading.Lock()
    execution_lock = threading.Lock()
    active: dict[int, threading.Thread] = {}
    canceled: set[int] = set()

    def send(value: dict) -> None:
        with output_lock:
            protocol_stdout.write(json.dumps(value) + "\n")
            protocol_stdout.flush()

    def run(request: dict) -> None:
        nonlocal environment
        identifier = request["id"]
        try:
            with execution_lock:
                if identifier in canceled:
                    send({"id": identifier, "output": "Command cancelled", "returncode": 130})
                    return
                if environment is None:
                    image = config.get(f"{backend}_image", "")
                    environment = _create_environment(
                        env_type=backend, image=image, cwd=config["cwd"], timeout=config["timeout"],
                        ssh_config=_ssh_config_from_config(config) if backend == "ssh" else None,
                        container_config=_container_config_from_config(config) if _is_container_backend(backend) else None,
                        task_id=f"omp-{os.getpid()}",
                    )
                if identifier in canceled:
                    send({"id": identifier, "output": "Command cancelled", "returncode": 130})
                    return
                command = request["command"]
                overlay = request.get("env") or {}
                if any(not isinstance(k, str) or not k.isidentifier() or not isinstance(v, str)
                       for k, v in overlay.items()):
                    raise ValueError("Invalid shell environment overlay")
                if overlay:
                    command = "export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in overlay.items()) + "; " + command
                timeout_ms = request.get("timeout", 300_000)
                stream_buffer = ""

                def stream(text: str) -> None:
                    nonlocal stream_buffer
                    stream_buffer += text
                    while "\n" in stream_buffer:
                        line, stream_buffer = stream_buffer.split("\n", 1)
                        if environment._cwd_marker not in line:
                            send({"id": identifier, "chunk": line + "\n"})
                    if len(stream_buffer) > 2048 and environment._cwd_marker not in stream_buffer:
                        send({"id": identifier, "chunk": stream_buffer})
                        stream_buffer = ""

                result = environment.execute(command, cwd=request.get("cwd") or "",
                    timeout=0 if timeout_ms == 0 else max(0.001, timeout_ms / 1000),
                    bounded_capture=True, on_output=stream)
                if stream_buffer and environment._cwd_marker not in stream_buffer:
                    send({"id": identifier, "chunk": stream_buffer})
                send({"id": identifier, **result, "cwd": environment.cwd})
        except Exception as error:
            send({"id": identifier, "error": f"Mercury {backend} terminal: {error}"})
        finally:
            set_interrupt(False)
            active.pop(identifier, None)
            canceled.discard(identifier)

    for line in sys.stdin:
        request = json.loads(line)
        if "cancel" in request:
            identifier = request["cancel"]
            canceled.add(identifier)
            thread = active.get(identifier)
            if thread and thread.ident:
                set_interrupt(True, thread.ident)
        else:
            thread = threading.Thread(target=run, args=(request,), daemon=True)
            active[request["id"]] = thread
            thread.start()
    # EOF is actual agent shutdown, not steering. Interrupt only this worker's
    # calls. Do not close shared SSH masters used by another engine/session.
    canceled.update(active)
    for thread in list(active.values()):
        if thread.ident:
            set_interrupt(True, thread.ident)
    for thread in list(active.values()):
        thread.join(timeout=3)
    if environment is not None and backend != "ssh":
        environment.cleanup()
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1])
