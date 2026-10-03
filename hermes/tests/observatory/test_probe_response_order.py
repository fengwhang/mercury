"""Membership diagnostics must tolerate outstanding auto-join replies."""
import asyncio

import pytest

from observatory.doctor import _Probe


@pytest.mark.asyncio
async def test_names_waits_for_fresh_response_and_filters_other_rooms():
    async def peer(reader, writer):
        try:
            while raw := await reader.readline():
                line = raw.decode().rstrip("\r\n")
                if line.startswith("USER "):
                    writer.write(b":server 001 probe :Welcome\r\n")
                elif line.startswith("JOIN "):
                    # A pending auto-join end reply without its discarded
                    # names payload must not be mistaken for an empty room.
                    writer.write(b":server 366 probe #target :End of names\r\n")
                elif line.startswith("PING "):
                    writer.write(f":server PONG server {line[5:]}\r\n".encode())
                elif line.startswith("NAMES "):
                    writer.write(b":server 353 probe = #other :unrelated\r\n"
                                 b":server 366 probe #other :End of names\r\n"
                                 b":server 353 probe = #target :@gateway probe\r\n"
                                 b":server 366 probe #target :End of names\r\n")
                elif line.startswith("QUIT "):
                    break
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    probe = _Probe("127.0.0.1", server.sockets[0].getsockname()[1], "probe", "")
    try:
        assert await asyncio.to_thread(probe.connect)
        assert await asyncio.to_thread(probe.names, "#target") == ["gateway", "probe"]
    finally:
        probe.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_names_rejects_partial_membership_response():
    async def peer(reader, writer):
        try:
            while raw := await reader.readline():
                line = raw.decode().rstrip("\r\n")
                if line.startswith("USER "):
                    writer.write(b":server 001 probe :Welcome\r\n")
                elif line.startswith("PING "):
                    writer.write(f":server PONG server {line[5:]}\r\n".encode())
                elif line.startswith("NAMES "):
                    writer.write(b":server 353 probe = #target :probe\r\n")
                    # No end-of-NAMES: this is incomplete, not an empty fleet.
                elif line.startswith("QUIT "):
                    break
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    probe = _Probe("127.0.0.1", server.sockets[0].getsockname()[1], "probe", "")
    try:
        assert await asyncio.to_thread(probe.connect)
        assert await asyncio.to_thread(probe.names, "#target", 0.2) is None
    finally:
        probe.close()
        server.close()
        await server.wait_closed()
