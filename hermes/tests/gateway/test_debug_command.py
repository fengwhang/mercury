"""The gateway never sends diagnostic content to reporting services or chat."""

import pytest
from gateway.slash_commands import GatewaySlashCommandsMixin


@pytest.mark.asyncio
async def test_debug_returns_local_command_guidance():
    result = await GatewaySlashCommandsMixin._handle_debug_command(object(), None)
    assert "mercury debug report" in result
    assert "--output report.txt" in result
    assert "not uploaded or sent through chat" in result
