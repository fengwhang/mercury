# Adapted from NousResearch/hermes-agent PM lifecycle tests.
"""Real artifacts and isolated PM workers shared by lifecycle tests."""
from __future__ import annotations

from functools import partial
from contextlib import contextmanager
import hashlib
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import io
from pathlib import Path
import tarfile
import threading
import zipfile

import pytest


def _wheel(directory: Path, name: str, version: str = "1.0", requirements=()) -> Path:
    metadata = f"{name}-{version}.dist-info"
    entries = {
        f"{name}/__init__.py": f"__version__ = {version!r}\n",
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements),
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    entries[f"{metadata}/RECORD"] = "".join(f"{path},,\n" for path in entries)
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, body in entries.items():
            archive.writestr(path, body)
    return wheel


def _ar_member(name: str, data: bytes) -> bytes:
    hdr = (
        name.ljust(16).encode()
        + b"0".ljust(12)
        + b"0".ljust(6)
        + b"0".ljust(6)
        + b"100644".ljust(8)
        + str(len(data)).encode().ljust(10)
        + b"`\n"
    )
    pad = b"\n" if len(data) % 2 else b""
    return hdr + data + pad



def make_tar(docroot: Path, name: str, files: dict[str, str]) -> tuple[str, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    payload = buf.getvalue()
    (docroot / name).write_bytes(payload)
    return name, hashlib.sha256(payload).hexdigest()


@contextmanager
def threaded_server(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def served(tmp_path):
    docroot = tmp_path / "www"
    docroot.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(docroot))
    with threaded_server(handler) as server:
        yield docroot, f"http://127.0.0.1:{server.server_port}"

