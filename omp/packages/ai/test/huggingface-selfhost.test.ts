import { describe, expect, test } from "bun:test";
import { authPolicyFor } from "@oh-my-pi/pi-catalog/compat/auth";
import { getBundledModels } from "@oh-my-pi/pi-catalog/models";
import { completeSimple } from "../src/index";
import { resolveOpenAIRequestSetup } from "../src/providers/openai-shared";
import { validateOpenAICompatibleApiKey } from "../src/registry/api-key-validation";
import { createApiKeyLogin } from "../src/registry/engine/api-key";
import type { FetchImpl } from "../src/types";

const bundled = getBundledModels("huggingface")[0]!;
const forbidden = [
	"",
	"https://router.huggingface.co/v1",
	"https://api-inference.huggingface.co/v1",
	"https://ROUTER.HUGGINGFACE.CO.:443/v1",
];
const context = { messages: [{ role: "user" as const, content: "ping", timestamp: 0 }] };

describe("Hugging Face inference and auth HTTP", () => {
	for (const baseUrl of forbidden) {
		test(`refuses cached endpoint ${baseUrl || "missing URL"} without HTTP or OpenAI fallback`, async () => {
			let requests = 0;
			const fetch: FetchImpl = async () => {
				requests++;
				throw new Error("unexpected HTTP");
			};
			const model = { ...bundled, baseUrl };
			expect(() =>
				resolveOpenAIRequestSetup(model, {
					apiKey: "test-key",
					messages: [],
					defaultBaseUrl: "https://api.openai.com/v1",
				}),
			).toThrow("self-hosted");
			const result = await completeSimple(model, context, { apiKey: "test-key", fetch });
			expect(result.errorMessage).toContain("self-hosted");
			expect(requests).toBe(0);
		});
		test(`refuses auth validation ${baseUrl || "missing URL"} before HTTP`, async () => {
			let requests = 0;
			const fetch: FetchImpl = async () => {
				requests++;
				throw new Error("unexpected HTTP");
			};
			await expect(
				validateOpenAICompatibleApiKey({
					provider: "Hugging Face",
					apiKey: "test-key",
					baseUrl,
					model: bundled.id,
					fetch,
				}),
			).rejects.toThrow("self-hosted");
			expect(requests).toBe(0);
		});
	}

	test("undefined endpoint is refused rather than inheriting OpenAI defaults", () => {
		expect(() =>
			resolveOpenAIRequestSetup(
				{ provider: "huggingface", id: bundled.id },
				{ apiKey: "test-key", messages: [], defaultBaseUrl: "https://api.openai.com/v1" },
			),
		).toThrow("self-hosted");
	});

	test("compiled login neither opens a hosted browser route nor performs network validation", async () => {
		const rule = authPolicyFor("huggingface")?.login;
		expect(rule?.kind).toBe("api-key");
		if (rule?.kind !== "api-key") throw new Error("Missing Hugging Face login rule");
		const routes: string[] = [];
		let requests = 0;
		const key = await createApiKeyLogin(rule, "Hugging Face Inference")({
			onPrompt: async () => "test-key",
			onAuth: info => routes.push(info.url),
			fetch: async () => {
				requests++;
				throw new Error("unexpected HTTP");
			},
		});
		expect(key).toBe("test-key");
		expect(routes).toEqual([]);
		expect(requests).toBe(0);
	});

	test("self-hosted inference and validation preserve the configured URL", async () => {
		const urls: string[] = [];
		const fetch: FetchImpl = async input => {
			urls.push(String(input));
			const chunk = (delta: unknown, finishReason: string | null) =>
				JSON.stringify({ id: "local", choices: [{ delta, finish_reason: finishReason }] });
			return new Response(
				`data: ${chunk({ content: "ok" }, null)}\n\ndata: ${chunk({}, "stop")}\n\ndata: [DONE]\n\n`,
				{ headers: { "content-type": "text/event-stream" } },
			);
		};
		const result = await completeSimple(
			{ ...bundled, baseUrl: "https://example.invalid/v1" },
			context,
			{ apiKey: "test-key", fetch },
		);
		expect(result.stopReason).toBe("stop");
		await validateOpenAICompatibleApiKey({
			provider: "Hugging Face",
			apiKey: "test-key",
			baseUrl: "https://example.invalid/v1",
			model: bundled.id,
			fetch,
		});
		expect(urls).toEqual([
			"https://example.invalid/v1/chat/completions",
			"https://example.invalid/v1/chat/completions",
		]);
	});
});
