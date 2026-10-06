import { describe, expect, test } from "bun:test";
import { fetchOpenAICompatibleModels } from "../src/discovery/openai-compatible";
import { createModelManager } from "../src/model-manager";
import {
	huggingfaceModelManagerOptions,
	mapModelsDevToModels,
	MODELS_DEV_PROVIDER_DESCRIPTORS,
} from "../src/provider-models/openai-compat";
import type { FetchImpl } from "../src/types";

const prohibited = [
	"",
	"https://router.huggingface.co/v1",
	"https://api-inference.huggingface.co/v1",
	"https://ROUTER.HUGGINGFACE.CO.:443/v1",
	"https://endpoint.endpoints.huggingface.cloud/v1",
	"https://hf.co/v1",
	"https://hf.space/v1",
	"https://APP.HF.SPACE.:443/v1",
	"https://hfusercontent.com/v1",
	"https://ASSET.HFUSERCONTENT.COM.:443/v1",
];

describe("Hugging Face self-hosted discovery", () => {
	for (const baseUrl of prohibited) {
		test(`refuses ${baseUrl || "missing base URL"} before HTTP`, async () => {
			let requests = 0;
			const fetch: FetchImpl = async () => {
				requests++;
				throw new Error("unexpected HTTP");
			};
			await expect(
				fetchOpenAICompatibleModels({
					api: "openai-completions",
					provider: "huggingface",
					apiKey: "test-key",
					baseUrl,
					fetch,
				}),
			).rejects.toThrow("self-hosted");
			expect(requests).toBe(0);
		});
	}

	test("manager discovery requires an explicit self-hosted base URL", async () => {
		let requests = 0;
		const options = huggingfaceModelManagerOptions({
			apiKey: "test-key",
			fetch: async () => {
				requests++;
				throw new Error("unexpected HTTP");
			},
		});
		await expect(options.fetchDynamicModels!()).rejects.toThrow("self-hosted");
		const result = await createModelManager(options).refresh("online");
		expect(result.stale).toBe(true);
		expect(requests).toBe(0);
	});

	test("manager preserves explicit self-hosted discovery transport", async () => {
		const requests: string[] = [];
		const fetch: FetchImpl = async input => {
			requests.push(String(input));
			return Response.json({ data: [{ id: "local-model" }] });
		};
		const options = huggingfaceModelManagerOptions({
			apiKey: "test-key",
			baseUrl: "https://example.invalid/v1",
			fetch,
		});
		const models = await options.fetchDynamicModels?.();
		expect(requests).toEqual(["https://example.invalid/v1/models"]);
		expect(models?.[0]?.baseUrl).toBe("https://example.invalid/v1");
	});

	test("authoritative metadata descriptor cannot regenerate the public endpoint", () => {
		const descriptor = MODELS_DEV_PROVIDER_DESCRIPTORS.find(entry => entry.providerId === "huggingface")!;
		expect(descriptor.baseUrl).toBe("");
		const models = mapModelsDevToModels({ huggingface: { models: { local: { tool_call: true } } } }, [descriptor]);
		expect(models[0]?.baseUrl).toBe("");
	});
});
