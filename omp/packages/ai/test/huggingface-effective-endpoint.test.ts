import { expect, test } from "bun:test";
import { getBundledModels } from "@oh-my-pi/pi-catalog/models";
import { completeSimple } from "../src/index";
import { resolveOpenAIRequestSetup } from "../src/providers/openai-shared";
import type { FetchImpl } from "../src/types";

const context = { messages: [{ role: "user" as const, content: "ping", timestamp: 0 }] };
const bundled = getBundledModels("huggingface")[0]!;
const hosted = "https://APP.HF.SPACE.:443/v1";

for (const [provider, env] of [
	["moonshot", "MOONSHOT_BASE_URL"],
	["sakana", "SAKANA_BASE_URL"],
	["custom", "OPENAI_BASE_URL"],
] as const) {
	test(`refuses effective ${env} override before inference HTTP`, async () => {
		const previous = Bun.env[env];
		Bun.env[env] = hosted;
		try {
			let requests = 0;
			const fetch: FetchImpl = async () => {
				requests++;
				throw new Error("unexpected HTTP");
			};
			const model = {
				...bundled,
				provider,
				baseUrl: provider === "custom" ? undefined : "https://example.invalid/v1",
			};
			expect(() =>
				resolveOpenAIRequestSetup(model, {
					apiKey: "test-key",
					messages: [],
					defaultBaseUrl: "https://example.invalid/v1",
				}),
			).toThrow("self-hosted");
			const result = await completeSimple(model as typeof bundled, context, { apiKey: "test-key", fetch });
			expect(result.errorMessage).toContain("self-hosted");
			expect(requests).toBe(0);
		} finally {
			if (previous === undefined) delete Bun.env[env];
			else Bun.env[env] = previous;
		}
	});
}

test("refuses an effective default URL for a custom alias", () => {
	expect(() =>
		resolveOpenAIRequestSetup(
			{ provider: "custom", id: "local" },
			{ apiKey: "test-key", messages: [], defaultBaseUrl: hosted },
		),
	).toThrow("self-hosted");
});

test("refuses an effective credential endpoint before inference HTTP", async () => {
	let requests = 0;
	const fetch: FetchImpl = async () => {
		requests++;
		throw new Error("unexpected HTTP");
	};
	const model = { ...bundled, provider: "alibaba-token-plan", baseUrl: "https://example.invalid/v1" };
	const apiKey = JSON.stringify({ token: "sk-test-key", baseUrl: hosted });
	expect(() => resolveOpenAIRequestSetup(model, { apiKey, messages: [] })).toThrow("self-hosted");
	const result = await completeSimple(model, context, { apiKey, fetch });
	expect(result.errorMessage).toContain("self-hosted");
	expect(requests).toBe(0);
});

test("preserves an explicit self-hosted environment override over an obsolete hosted model URL", async () => {
	const previous = Bun.env.MOONSHOT_BASE_URL;
	Bun.env.MOONSHOT_BASE_URL = "https://example.invalid/v1";
	try {
		const urls: string[] = [];
		const model = { ...bundled, provider: "moonshot", baseUrl: hosted };
		const result = await completeSimple(model, context, {
			apiKey: "test-key",
			fetch: async input => {
				urls.push(String(input));
				return new Response(
					'data: {"id":"local","choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n' +
						'data: {"id":"local","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n' +
						"data: [DONE]\n\n",
					{ headers: { "content-type": "text/event-stream" } },
				);
			},
		});
		expect(result.stopReason).toBe("stop");
		expect(urls).toEqual(["https://example.invalid/v1/chat/completions"]);
	} finally {
		if (previous === undefined) delete Bun.env.MOONSHOT_BASE_URL;
		else Bun.env.MOONSHOT_BASE_URL = previous;
	}
});
