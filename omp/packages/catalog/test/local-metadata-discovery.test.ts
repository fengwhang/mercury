import { describe, expect, test, vi } from "bun:test";
import { getBundledModelReferenceIndex } from "@oh-my-pi/pi-catalog/identity/bundled";
import { resolveModelReference } from "@oh-my-pi/pi-catalog/identity/reference";
import { getBundledModels, getBundledProviders } from "@oh-my-pi/pi-catalog/models";
import {
	anthropicModelManagerOptions,
	litellmModelManagerOptions,
	opencodeZenModelManagerOptions,
	siliconflowModelManagerOptions,
} from "@oh-my-pi/pi-catalog/provider-models/openai-compat";
import type { FetchImpl } from "@oh-my-pi/pi-catalog/types";
import { loadPreviousSnapshotModels } from "../scripts/generate-models";

describe("local metadata discovery", () => {
	test("generator metadata load preserves the complete local snapshot without transport", () => {
		const transport = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
			throw new Error("metadata loading must not use network");
		});
		try {
			const models = loadPreviousSnapshotModels();
			const bundled = getBundledProviders().flatMap(provider => getBundledModels(provider));
			expect(models.length).toBe(bundled.length);
			expect(models.length).toBeGreaterThan(100);
			expect(models.map(model => `${model.provider}/${model.id}`).sort()).toEqual(
				bundled.map(model => `${model.provider}/${model.id}`).sort(),
			);
			const opus = models.find(model => model.provider === "anthropic" && model.id === "claude-opus-5");
			expect(opus?.input).toContain("image");
			expect(opus?.contextWindow).toBeGreaterThan(100_000);
			expect(transport).not.toHaveBeenCalled();
		} finally {
			transport.mockRestore();
		}
	});

	test("Anthropic requests only the selected provider and keeps bundled vision and limits", async () => {
		const reference = getBundledModels("anthropic").find(model => model.id === "claude-opus-5");
		if (!reference) throw new Error("Bundled Anthropic reference missing");
		const urls: string[] = [];
		const transport: FetchImpl = async input => {
			const url = String(input);
			urls.push(url);
			if (url !== "https://anthropic.operator.test/v1/models") return new Response("unexpected", { status: 404 });
			return Response.json({ data: [{ id: reference.id, display_name: "Operator Opus" }] });
		};
		const options = anthropicModelManagerOptions({
			apiKey: "test-key",
			baseUrl: "https://anthropic.operator.test",
			fetch: transport,
		});
		const models = await options.fetchDynamicModels?.();
		expect(urls).toEqual(["https://anthropic.operator.test/v1/models"]);
		expect(models?.find(model => model.id === reference.id)).toMatchObject({
			input: reference.input,
			reasoning: reference.reasoning,
			contextWindow: reference.contextWindow,
			maxTokens: reference.maxTokens,
			cost: reference.cost,
			baseUrl: "https://anthropic.operator.test",
		});
	});

	test("SiliconFlow enriches canonical capabilities without copying another provider's pricing", async () => {
		const id = "deepseek-ai/DeepSeek-V4-Pro";
		const reference = resolveModelReference(id, getBundledModelReferenceIndex());
		if (!reference) throw new Error("Bundled DeepSeek reference missing");
		const urls: string[] = [];
		const transport: FetchImpl = async input => {
			const url = String(input);
			urls.push(url);
			if (url !== "https://siliconflow.operator.test/v1/models") return new Response("unexpected", { status: 404 });
			return Response.json({ data: [{ id }] });
		};
		const models = await siliconflowModelManagerOptions({
			apiKey: "test-key",
			baseUrl: "https://siliconflow.operator.test/v1",
			fetch: transport,
		}).fetchDynamicModels?.();
		expect(urls).toEqual(["https://siliconflow.operator.test/v1/models"]);
		expect(models?.[0]).toMatchObject({
			id,
			reasoning: true,
			contextWindow: reference.contextWindow,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		});
	});

	test("OpenCode preserves bundled metadata and operator routing with provider-only discovery", async () => {
		const reference = getBundledModels("opencode-zen").find(model => model.contextWindow != null && model.reasoning);
		if (!reference) throw new Error("Bundled OpenCode reference missing");
		const urls: string[] = [];
		const transport: FetchImpl = async input => {
			urls.push(String(input));
			return Response.json({ data: [{ id: reference.id }] });
		};
		const options = opencodeZenModelManagerOptions({
			apiKey: "test-key",
			baseUrl: "https://opencode.operator.test/v1",
			fetch: transport,
		});
		const models = await options.fetchDynamicModels?.();
		expect(options.modelsDev).toBeUndefined();
		expect(urls).toEqual(["https://opencode.operator.test/v1/models"]);
		expect(models?.[0]).toMatchObject({
			id: reference.id,
			reasoning: reference.reasoning,
			contextWindow: reference.contextWindow,
		});
	});

	test("LiteLLM fallback keeps local canonical metadata and only queries the selected proxy", async () => {
		const id = "deepseek-v4-pro";
		const reference = resolveModelReference(id, getBundledModelReferenceIndex());
		if (!reference) throw new Error("Bundled LiteLLM reference missing");
		const urls: string[] = [];
		const transport: FetchImpl = async input => {
			const url = String(input);
			urls.push(url);
			if (url === "https://litellm.operator.test/v1/models") return Response.json({ data: [{ id }] });
			return new Response("no management metadata", { status: 404 });
		};
		const models = await litellmModelManagerOptions({
			baseUrl: "https://litellm.operator.test/v1",
			fetch: transport,
		}).fetchDynamicModels?.();
		expect(urls.length).toBeGreaterThan(0);
		expect(urls.every(url => new URL(url).hostname === "litellm.operator.test")).toBe(true);
		expect(models?.[0]).toMatchObject({ id, reasoning: true, contextWindow: reference.contextWindow });
	});
});
