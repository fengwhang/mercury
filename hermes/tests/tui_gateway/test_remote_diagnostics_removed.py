"""Remote diagnostic reporting is not a Mercury RPC capability."""

from tui_gateway import server


def test_remote_diagnostics_rpc_is_unknown():
    response = server.dispatch({
        "jsonrpc": "2.0", "id": "diagnostics", "method": "diagnostics.share_nous",
        "params": {"error_context": "private diagnostic text"},
    })
    assert response["error"]["code"] == -32601
    assert "result" not in response
