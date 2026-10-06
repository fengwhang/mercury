import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";
import { fetchReadUrl } from "@oh-my-pi/pi-coding-agent/tools/fetch";
import { removeSyncWithRetries, Snowflake } from "@oh-my-pi/pi-utils";
import { asGlobalFetch } from "../helpers/fetch-mock";

const PAGE_HTML =
	"<html><head><title>Resource page</title></head><body><main>Explicitly requested page</main></body></html>";
const PAGE_MARKDOWN =
	"# Resource page\n\nThis content comes from the explicitly requested page using ordinary HTTP content negotiation, not a vendor API or raw README endpoint.";

function pageResponse(url: string, body: string, contentType: string, status = 200): Response {
	const response = new Response(body, { status, headers: { "Content-Type": contentType } });
	Object.defineProperty(response, "url", { value: url });
	return response;
}

describe("explicit Hugging Face URLs use generic web reading", () => {
	let testDir: string;

	beforeEach(() => {
		testDir = path.join(os.tmpdir(), `fetch-huggingface-generic-${Snowflake.next()}`);
		fs.mkdirSync(testDir, { recursive: true });
	});

	afterEach(() => {
		vi.restoreAllMocks();
		removeSyncWithRetries(testDir);
	});

	it.each([
		"https://huggingface.co/google/bert_uncased_L-2_H-128_A-2",
		"https://huggingface.co/bert-base-uncased",
		"https://huggingface.co/datasets/stanfordnlp/squad",
		"https://huggingface.co/datasets/squad",
		"https://huggingface.co/spaces/gradio/hello_world",
		"https://huggingface.co/some-user",
	])("reads %s without implicit API or raw README requests", async url => {
		const session: ToolSession = {
			cwd: testDir,
			hasUI: false,
			getSessionFile: () => null,
			getSessionSpawns: () => null,
			settings: Settings.isolated({ "fetch.enabled": true }),
		};
		const requests: string[] = [];
		vi.spyOn(globalThis, "fetch").mockImplementation(
			asGlobalFetch((input, init) => {
				const requestedUrl = input instanceof Request ? input.url : String(input);
				requests.push(requestedUrl);
				if (requestedUrl !== url) {
					return pageResponse(requestedUrl, "Not found", "text/plain", 404);
				}
				const accept = new Headers(init?.headers).get("Accept") ?? "";
				return accept.startsWith("text/markdown")
					? pageResponse(url, PAGE_MARKDOWN, "text/markdown")
					: pageResponse(url, PAGE_HTML, "text/html");
			}),
		);

		// Exercise the real registry, loadPage and generic HTML dispatch. Only the
		// HTTP transport is fake; no special handler or generic reader is stubbed.
		const result = await fetchReadUrl(session, { path: url });

		expect(result.details.method).toBe("content-negotiation");
		expect(result.details.finalUrl).toBe(url);
		expect(result.details.contentType).toBe("text/markdown");
		expect(result.content).toBe(PAGE_MARKDOWN);
		// The ordinary .md probe and content negotiation remain available. This
		// exact request sequence also excludes all implicit vendor API/README calls.
		expect(requests).toEqual([url, `${url}.md`, url]);
	});
});
