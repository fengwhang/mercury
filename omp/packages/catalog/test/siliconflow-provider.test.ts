import { describe, expect, test } from "bun:test";
import { getOAuthProviders } from "@oh-my-pi/pi-ai/registry/oauth";
import { getEnvApiKey } from "@oh-my-pi/pi-ai/stream";
import { getBundledModelReferenceIndex } from "@oh-my-pi/pi-catalog/identity/bundled";
import { resolveModelReference } from "@oh-my-pi/pi-catalog/identity/reference";
import type { ProviderCatalogEntry } from "@oh-my-pi/pi-catalog/provider-models/descriptor-types";
import {
	CATALOG_PROVIDERS,
	DEFAULT_MODEL_PER_PROVIDER,
	PROVIDER_DESCRIPTORS,
} from "@oh-my-pi/pi-catalog/provider-models/descriptors";
import {
	MODELS_DEV_PROVIDER_DESCRIPTORS,
	siliconflowCnModelManagerOptions,
	siliconflowModelManagerOptions,
} from "@oh-my-pi/pi-catalog/provider-models/openai-compat";
import type { FetchImpl } from "@oh-my-pi/pi-catalog/types";

function withEnv(key: string, value: string, run: () => void): void {
	const previous = Bun.env[key];
	Bun.env[key] = value;
	try {
		run();
	} finally {
		if (previous === undefined) {
			delete Bun.env[key];
		} else {
			Bun.env[key] = previous;
		}
	}
}

describe("siliconflow built-in providers", () => {
	test("registers dynamic-authoritative runtime descriptors with env-key discovery", () => {
		const intl = PROVIDER_DESCRIPTORS.find(item => item.providerId === "siliconflow");
		expect(intl).toBeDefined();
		expect(intl?.defaultModel).toBe("zai-org/GLM-5.1");
		expect(intl?.dynamicModelsAuthoritative).toBe(true);
		expect(DEFAULT_MODEL_PER_PROVIDER.siliconflow).toBe("zai-org/GLM-5.1");

		const cn = PROVIDER_DESCRIPTORS.find(item => item.providerId === "siliconflow-cn");
		expect(cn).toBeDefined();
		expect(cn?.defaultModel).toBe("deepseek-ai/DeepSeek-V4-Pro");
		expect(cn?.dynamicModelsAuthoritative).toBe(true);
		expect(DEFAULT_MODEL_PER_PROVIDER["siliconflow-cn"]).toBe("deepseek-ai/DeepSeek-V4-Pro");
	});

	test("ships no bundled catalog — the model list is discovered live", () => {
		// Source of truth: the catalog table owns generator participation via
		// `catalogDiscovery` — the SiliconFlow entries are dynamic-authoritative
		// and deliberately carry no catalog discovery config.
		for (const providerId of ["siliconflow", "siliconflow-cn"] as const) {
			const entry: ProviderCatalogEntry | undefined = CATALOG_PROVIDERS.find(item => item.id === providerId);
			expect(entry).toBeDefined();
			expect(entry?.dynamicModelsAuthoritative).toBe(true);
			expect(entry?.catalogDiscovery).toBeUndefined();
		}
		// No raw metadata mapping may feed generation either.
		expect(MODELS_DEV_PROVIDER_DESCRIPTORS.some(d => d.providerId === "siliconflow")).toBe(false);
		expect(MODELS_DEV_PROVIDER_DESCRIPTORS.some(d => d.providerId === "siliconflow-cn")).toBe(false);
	});

	test("registers API-key login providers", () => {
		const providers = getOAuthProviders();
		const intl = providers.find(item => item.id === "siliconflow");
		expect(intl?.name).toBe("SiliconFlow");
		expect(intl?.available).toBe(true);
		const cn = providers.find(item => item.id === "siliconflow-cn");
		expect(cn?.name).toBe("SiliconFlow (China)");
		expect(cn?.available).toBe(true);
	});

	test("resolves SILICONFLOW_API_KEY / SILICONFLOW_CN_API_KEY via env", () => {
		withEnv("SILICONFLOW_API_KEY", "siliconflow-test-key", () => {
			expect(getEnvApiKey("siliconflow")).toBe("siliconflow-test-key");
		});
		withEnv("SILICONFLOW_CN_API_KEY", "siliconflow-cn-test-key", () => {
			expect(getEnvApiKey("siliconflow-cn")).toBe("siliconflow-cn-test-key");
		});
	});

	test("dynamic discovery filters non-chat ids and enriches from bundled references only", async () => {
		const seen: { urls: string[]; authorization?: string } = { urls: [] };
		const stubFetch: FetchImpl = async (input, init) => {
			seen.urls.push(String(input));
			seen.authorization = new Headers(init?.headers).get("Authorization") ?? undefined;
			return Response.json({
				data: [
					{ id: "zai-org/GLM-5.1" },
					{ id: "deepseek-ai/DeepSeek-V4-Pro" },
					{ id: "BAAI/bge-m3" },
					{ id: "Qwen/Qwen-Image" },
					{ id: "Wan-AI/Wan2.2-T2V-A14B" },
					{ id: "TeleAI/TeleSpeechASR" },
					{ id: "IndexTeam/IndexTTS-2" },
				],
			});
		};
		const options = siliconflowModelManagerOptions({ apiKey: "sk-test", fetch: stubFetch });
		expect(options.dynamicModelsAuthoritative).toBe(true);
		const models = await options.fetchDynamicModels?.();
		expect(models?.map(model => model.id)).toEqual(["deepseek-ai/DeepSeek-V4-Pro", "zai-org/GLM-5.1"]);
		for (const model of models ?? []) {
			const canonical = resolveModelReference(model.id, getBundledModelReferenceIndex());
			expect(canonical).toBeDefined();
			expect(model).toMatchObject({
				reasoning: canonical?.reasoning,
				contextWindow: canonical?.contextWindow,
				maxTokens:
					canonical?.maxTokens != null && canonical.contextWindow != null
						? Math.min(canonical.maxTokens, canonical.contextWindow)
						: (canonical?.maxTokens ?? null),
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				provider: "siliconflow",
				baseUrl: "https://api.siliconflow.com/v1",
			});
		}
		expect(seen.urls).toEqual(["https://api.siliconflow.com/v1/models"]);
		expect(seen.authorization).toBe("Bearer sk-test");
	});

	test("cn variant keeps canonical capabilities without inventing cn pricing", async () => {
		const urls: string[] = [];
		const stubFetch: FetchImpl = async input => {
			urls.push(String(input));
			return Response.json({ data: [{ id: "Pro/zai-org/GLM-5.1" }] });
		};
		const models = await siliconflowCnModelManagerOptions({
			apiKey: "sk-test",
			fetch: stubFetch,
		}).fetchDynamicModels?.();
		expect(models).toHaveLength(1);
		expect(models?.[0]).toMatchObject({
			id: "Pro/zai-org/GLM-5.1",
			reasoning: true,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			baseUrl: "https://api.siliconflow.cn/v1",
		});
		expect(urls).toEqual(["https://api.siliconflow.cn/v1/models"]);
	});
});
