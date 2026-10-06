import { expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { completeSimple } from "@oh-my-pi/pi-ai";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { writeModelCache } from "@oh-my-pi/pi-catalog/model-cache";
import { getBundledProviders } from "@oh-my-pi/pi-catalog/models";
import { PROVIDER_DESCRIPTORS } from "@oh-my-pi/pi-catalog/provider-models";
import {
	discoverLlamaCppModels,
	discoverLlamaCppModelRuntimeMetadata,
	discoverLiteLLMModels,
	discoverLmStudioModelRuntimeMetadata,
	discoverOllamaModels,
	discoverOpenAIModelsList,
	discoverProxyModels,
	type DiscoveryContext,
} from "../src/config/model-discovery";
import { ModelRegistry } from "../src/config/model-registry";
import { Settings } from "../src/config/settings";
import { AuthStorage } from "../src/session/auth-storage";

const hosted = [
	"https://ROUTER.HUGGINGFACE.CO.:443/v1",
	"https://API.HF.CO.:443/v1",
	"https://API.HUGGINGFACE.CLOUD.:443/v1",
	"https://APP.HF.SPACE.:443/v1",
	"https://ASSET.HFUSERCONTENT.COM.:443/v1",
];
const context = { messages: [{ role: "user" as const, content: "ping", timestamp: 0 }] };

for (const baseUrl of hosted) {
	for (const discover of [
		discoverOllamaModels,
		discoverLlamaCppModels,
		discoverOpenAIModelsList,
		discoverLiteLLMModels,
		discoverProxyModels,
	]) {
		test(`${discover.name} refuses custom hosted alias ${baseUrl} before HTTP`, async () => {
			let requests = 0;
			const ctx: DiscoveryContext = {
				fetch: async () => {
					requests++;
					throw new Error("unexpected HTTP");
				},
				getBearerApiKeyResolver: async () => undefined,
			};
			await expect(
				discover(
					{
						provider: "custom-alias",
						api: "openai-completions",
						baseUrl,
						discovery: { type: "openai-models-list" },
					},
					ctx,
				),
			).rejects.toThrow("self-hosted");
			expect(requests).toBe(0);
		});
	}
	for (const discover of [discoverLlamaCppModelRuntimeMetadata, discoverLmStudioModelRuntimeMetadata]) {
		test(`${discover.name} refuses old model endpoint ${baseUrl} before HTTP`, async () => {
			let requests = 0;
			const ctx: DiscoveryContext = {
				fetch: async () => {
					requests++;
					throw new Error("unexpected HTTP");
				},
				getBearerApiKeyResolver: async () => undefined,
			};
			await expect(
				discover({ provider: "custom-alias", id: "cached-model", baseUrl, maxTokens: 1024 }, ctx),
			).rejects.toThrow("self-hosted");
			expect(requests).toBe(0);
		});
	}

	test(`registry startup and runtime no-auth discovery refuses ${baseUrl}, including a stale cached alias`, async () => {
		const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "mercury-hf-registry-"));
		const auth = await AuthStorage.create(":memory:");
		try {
			const modelsPath = path.join(tempDir, "models.json");
			const cacheDbPath = path.join(tempDir, "models.db");
			fs.writeFileSync(
				modelsPath,
				JSON.stringify({
					providers: {
						"custom-alias": {
							baseUrl,
							auth: "none",
							api: "openai-completions",
							discovery: { type: "openai-models-list" },
						},
					},
				}),
			);
			const cached = buildModel({
				provider: "custom-alias",
				id: "cached-model",
				name: "cached-model",
				api: "openai-completions",
				baseUrl,
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 4096,
				maxTokens: 1024,
			});
			writeModelCache(
				"custom-alias:openai-models-list-context-v3",
				Date.now() - 48 * 60 * 60 * 1000,
				[cached],
				false,
				"",
				cacheDbPath,
			);
			let requests = 0;
			const fetch = async () => {
				requests++;
				throw new Error("unexpected HTTP");
			};
			// Scope startup discovery to the configured alias, not unrelated catalog providers.
			const settings = Settings.isolated({
				disabledProviders: [
					...getBundledProviders(),
					...PROVIDER_DESCRIPTORS.map(descriptor => descriptor.providerId),
					"ollama",
					"llama.cpp",
					"lm-studio",
				],
			});
			const registry = new ModelRegistry(auth, modelsPath, { cacheDbPath, fetch, settings });
			await registry.refresh("online-if-uncached");
			expect(registry.getProviderDiscoveryState("custom-alias")?.error).toContain("self-hosted");
			expect(requests).toBe(0);
			await registry.refreshProvider("custom-alias", "online");
			expect(registry.getProviderDiscoveryState("custom-alias")?.error).toContain("self-hosted");
			const result = await completeSimple(cached, context, { apiKey: "N/A", fetch });
			expect(result.errorMessage).toContain("self-hosted");
			expect(requests).toBe(0);
		} finally {
			auth.close();
			fs.rmSync(tempDir, { recursive: true, force: true });
		}
	});
}

test("configured discovery retains an explicit self-hosted endpoint", async () => {
	const urls: string[] = [];
	const models = await discoverOpenAIModelsList(
		{
			provider: "custom-alias",
			api: "openai-completions",
			baseUrl: "https://example.invalid/v1",
			discovery: { type: "openai-models-list" },
		},
		{
			fetch: async input => {
				urls.push(String(input));
				return Response.json({ data: [{ id: "local-model" }] });
			},
			getBearerApiKeyResolver: async () => undefined,
		},
	);
	expect(urls).toEqual(["https://example.invalid/v1/models"]);
	expect(models[0]?.baseUrl).toBe("https://example.invalid/v1");
});
