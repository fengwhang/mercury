"""omp-bridged web providers (MERCURY-OMP PATCH: tool-provider union).

Set-union of mercury' and omp's web tool providers where the types overlap:
mercury gains EVERY omp search provider (zai, kagi, perplexity, kimi,
tavily, brave, …) through a single bridge — omp's own provider registry,
invoked headlessly via its `__omp_worker_bridge_search` argv selector
(one JSON request on stdin, one JSON response on stdout). No per-provider
ports to keep in sync: the natural consequence is zai-search-in-mercury
plus everything else omp ships, today and on every future re-pin.

Scrape rides the same bridge (op=scrape → omp's firecrawl engine); both
engines read the same env keys (ZAI_API_KEY, FIRECRAWL_API_KEY, …) — one
key per capability, both engines.

Config (unified file, mercury: subtree)::

    web:
      search_backend: "omp-bridge:zai"     # any omp provider id after the colon
      extract_backend: "omp-bridge"        # scrape via omp's engine
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

BRIDGE_SELECTOR = "__omp_worker_bridge_search"
DEFAULT_TIMEOUT = 60


def _omp_binary() -> Optional[str]:
    env_bin = os.environ.get("HERMES_OMP_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    repo = os.environ.get("MERCURY_REPO", "").strip()
    if not repo:
        home = os.path.expanduser("~")
        for cand in (os.path.join(home, "Documents", "mercury-omp"),
                     os.path.join(home, "mercury-omp")):
            if os.path.isfile(os.path.join(cand, "omp", "packages", "coding-agent", "dist", "omp")):
                repo = cand
                break
    if repo:
        vendored = os.path.join(repo, "omp", "packages", "coding-agent", "dist", "omp")
        if os.path.isfile(vendored):
            return vendored
    return None


def _bridge_call(request: Dict[str, Any], timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    omp = _omp_binary()
    if not omp:
        raise RuntimeError("omp binary not found (set HERMES_OMP_BIN or build the vendored tree)")
    proc = subprocess.run(
        [omp, BRIDGE_SELECTOR],
        input=json.dumps(request), capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"omp bridge exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        raise RuntimeError("omp bridge produced no output")
    return json.loads(out[-1])


class OmpBridgeSearchProvider(WebSearchProvider):
    """Proxy provider: routes mercury web_search through omp's registry.

    The provider id after ``omp-bridge:`` selects omp's provider; the
    registered ``name`` is per-provider (e.g. ``omp-bridge:zai``) so the
    config key reads naturally and multiple bridge providers can coexist.
    """

    def __init__(self, omp_provider: str = "auto"):
        self._omp_provider = omp_provider

    @property
    def name(self) -> str:
        return f"omp-bridge:{self._omp_provider}"

    @property
    def display_name(self) -> str:
        return f"omp bridge ({self._omp_provider})"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        return _omp_binary() is not None

    def is_keyless_available(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            resp = _bridge_call({
                "op": "search",
                "provider": self._omp_provider,
                "query": query,
                "limit": limit,
                "timeoutMs": 45000,
            }, timeout=90)
        except Exception as e:
            return {"success": False, "error": str(e)}
        if not resp.get("ok"):
            return {"success": False, "error": str(resp.get("error", "bridge error"))[:400]}
        results = resp.get("results") or {}
        sources = results.get("sources") or []
        rows: List[Dict[str, Any]] = []
        for i, s in enumerate(sources[:limit], start=1):
            if not isinstance(s, dict):
                continue
            url = (s.get("url") or s.get("link") or "").strip()
            if not url:
                continue
            rows.append({
                "title": (s.get("title") or url)[:300],
                "url": url,
                "description": (s.get("snippet") or s.get("content") or "")[:500],
                "position": i,
            })
        if not rows:
            return {"success": False, "error": f"omp bridge returned no results: {json.dumps(results)[:200]}"}
        return {"success": True, "data": {"web": rows}}

    def extract(self, urls: List[str], **kwargs: Any) -> Any:
        """Scrape via omp's firecrawl engine (same FIRECRAWL_API_KEY)."""
        results = []
        for url in urls:
            try:
                resp = _bridge_call({"op": "scrape", "url": url}, timeout=120)
            except Exception as e:
                results.append({"url": url, "error": str(e)})
                continue
            if resp.get("ok"):
                results.append({"url": url, "content": resp.get("markdown") or ""})
            else:
                results.append({"url": url, "error": str(resp.get("error", ""))[:300]})
        return {"success": all("error" not in r for r in results), "data": {"results": results}}


class OmpBridgeZaiSearchProvider(OmpBridgeSearchProvider):
    """`omp-bridge:zai` — the flagship instance (one ZAI_API_KEY, both engines)."""

    def __init__(self) -> None:
        super().__init__("zai")

    @property
    def name(self) -> str:
        return "zai"

    @property
    def display_name(self) -> str:
        return "Z.AI Web Search (omp bridge)"
