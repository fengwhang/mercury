"""omp-bridged web providers (Mercury tool-provider union)."""
from plugins.web.omp_bridge.provider import (
    OmpBridgeSearchProvider,
    OmpBridgeZaiSearchProvider,
)


def register(ctx):
    # The flagship: zai search in mercury via omp's registry (one key both engines)
    ctx.register_web_search_provider(OmpBridgeZaiSearchProvider())
    # The general form: omp-bridge:<provider> for any omp provider
    ctx.register_web_search_provider(OmpBridgeSearchProvider("auto"))
