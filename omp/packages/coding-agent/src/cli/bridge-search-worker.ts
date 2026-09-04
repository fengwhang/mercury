/**
 * HERMES-OMP PATCH (tool-provider union): bridge worker for the hermes side.
 *
 * Hidden argv selector `__omp_worker_bridge_search` on the omp CLI: reads ONE JSON
 * request on stdin, performs the requested web operation via omp's OWN
 * provider registry (search or scrape), writes ONE JSON response on stdout,
 * exits. Lets hermes (or any external process) use omp's full search/scrape
 * provider set — zai, kagi, perplexity, kimi, firecrawl, … — without
 * reimplementing any of them. The natural consequence (user insight,
 * 2026-09-04): set-union of overlapping tool providers means hermes gains
 * zai search (and everything else omp has) with zero per-provider ports.
 *
 * Request:  {"op": "list"} | {"op": "search", "provider": "zai",
 *            "query": "...", "limit": 5} | {"op": "scrape",
 *            "provider": "firecrawl", "url": "...", ...}
 * Response: {"ok": true, ...result} | {"ok": false, "error": "..."}
 */
import { createRequire } from "node:module";
import process from "node:process";

interface BridgeRequest {
	op: "list" | "search" | "scrape";
	provider?: string;
	query?: string;
	url?: string;
	limit?: number;
	timeoutMs?: number;
	[key: string]: unknown;
}

async function readStdinJson(): Promise<BridgeRequest> {
	const chunks: Buffer[] = [];
	for await (const chunk of process.stdin) {
		chunks.push(Buffer.from(chunk));
	}
	const raw = Buffer.concat(chunks).toString("utf8").trim();
	if (!raw) throw new Error("empty request");
	return JSON.parse(raw) as BridgeRequest;
}

async function runBridgeSearchWorker(): Promise<void> {
	let request: BridgeRequest;
	try {
		request = await readStdinJson();
	} catch (error) {
		process.stdout.write(JSON.stringify({ ok: false, error: `bad request: ${String(error)}` }) + "\n");
		return;
	}

	try {
		if (request.op === "list") {
			const { SEARCH_PROVIDER_OPTIONS } = await import("../web/search/types");
			process.stdout.write(
				JSON.stringify({ ok: true, providers: SEARCH_PROVIDER_OPTIONS }) + "\n",
			);
			return;
		}

		if (request.op === "search") {
			const providerId = String(request.provider ?? "").trim();
			if (!providerId) throw new Error("search requires 'provider'");
			const query = String(request.query ?? "").trim();
			if (!query) throw new Error("search requires 'query'");

			const { getSearchProvider } = await import("../web/search/provider");
			const { AuthStorage } = await import("@oh-my-pi/pi-ai");
			const provider = await getSearchProvider(providerId as never);
			const limit = Number(request.limit ?? 5) || 5;
			const timeoutMs = Number(request.timeoutMs ?? 30000) || 30000;

			// Env credentials only: a minimal in-memory AuthCredentialStore —
			// providers resolve keys from the environment the caller exported
			// (ZAI_API_KEY, BRAVE_API_KEY, …) via getEnvApiKey; the store
			// exists to satisfy the authStorage contract, not to persist.
			const memStore = {
				_data: new Map<string, string>(),
				get(key: string) { return this._data.get(key) ?? null; },
				set(key: string, value: string) { this._data.set(key, value); },
				delete(key: string) { this._data.delete(key); },
				list() { return [...this._data.keys()]; },
				close() {},
			};
			const authStorage = new AuthStorage(memStore as never);
			const signal = AbortSignal.timeout(timeoutMs);
			const results = await provider.search({
				query,
				numResults: limit,
				signal,
				authStorage,
			} as never);
			process.stdout.write(JSON.stringify({ ok: true, provider: providerId, results }) + "\n");
			return;
		}

		if (request.op === "scrape") {
			// Scraping rides omp's firecrawl helper (the same scrape engine the
			// fetch tool uses when FIRECRAWL_API_KEY is set). One key, both
			// engines — hermes' own firecrawl plugin reads the same env var.
			const url = String(request.url ?? "").trim();
			if (!url) throw new Error("scrape requires 'url'");
			const { scrapeWithFirecrawl } = await import("../web/firecrawl");
			const markdown = await scrapeWithFirecrawl(url, { formats: ["markdown"] } as never, null);
			process.stdout.write(JSON.stringify({ ok: true, provider: "firecrawl", markdown }) + "\n");
			return;
		}

		throw new Error(`unknown op: ${String(request.op)}`);
	} catch (error) {
		process.stdout.write(JSON.stringify({ ok: false, error: String(error) }) + "\n");
	}
}

// Exported for the cli.ts selector dispatch.
export { runBridgeSearchWorker };
