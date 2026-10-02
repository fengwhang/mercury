import { describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { streamSimple } from "@oh-my-pi/pi-ai/stream";
import { transformRequestBody } from "@oh-my-pi/pi-ai/providers/openai-codex/request-transformer";
import {
	applyChatCompletionsCompatPolicy,
	applyResponsesCompatPolicy,
	type OpenAICompletionsParams,
	resolveOpenAICompatPolicy,
} from "@oh-my-pi/pi-ai/providers/openai-shared";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { fetchCodexModels } from "@oh-my-pi/pi-catalog/discovery/codex";
import { Effort, THINKING_EFFORTS } from "@oh-my-pi/pi-catalog/effort";
import { resolveProviderModels } from "@oh-my-pi/pi-catalog/model-manager";
import { clampThinkingLevelForModel, getSupportedEfforts } from "@oh-my-pi/pi-catalog/model-thinking";
import { openrouterModelManagerOptions } from "@oh-my-pi/pi-catalog/provider-models/openai-compat";
import { openaiCodexModelManagerOptions } from "@oh-my-pi/pi-catalog/provider-models/special";
import type { ModelSpec } from "@oh-my-pi/pi-catalog/types";
import { ThinkingLevel } from "@oh-my-pi/pi-agent-core";
import { resolveThinkingLevelForModel } from "@oh-my-pi/pi-coding-agent/thinking";

function catalogFetch(payload: object): typeof fetch {
	return Object.assign(async () => Response.json(payload), { preconnect() {} });
}

async function codexSpecs(): Promise<ModelSpec<"openai-codex-responses">[]> {
	const discovered = await fetchCodexModels({
		accessToken: "test-token",
		baseUrl: "https://codex.example/backend-api",
		fetchFn: catalogFetch({
			models: [
				{
					slug: "gpt-6.1-sol",
					supported_reasoning_levels: [{ effort: "high" }, { effort: "low" }],
					default_reasoning_level: "high",
				},
				{
					slug: "gpt-5.3-codex",
					supported_reasoning_levels: ["none", "minimal", "max"],
					default_reasoning_level: "minimal",
				},
				{ slug: "gpt-5.6-sol", supported_reasoning_levels: ["none"], default_reasoning_level: "none" },
			],
		}),
	});
	if (!discovered) throw new Error("Expected Codex catalog");
	return discovered.models;
}

describe("API-advertised runtime reasoning", () => {
	it("uses advertised levels through the engine's streaming request path", async () => {
		const specs = await codexSpecs();
		const tokenPayload = Buffer.from(
			JSON.stringify({ "https://api.openai.com/auth": { chatgpt_account_id: "test-account" } }),
		).toBase64();
		for (const [id, requested, expected] of [
			["gpt-6.1-sol", Effort.Max, "high"],
			["gpt-5.3-codex", Effort.Max, "max"],
			["gpt-5.3-codex", Effort.Minimal, "minimal"],
			["gpt-6.1-sol", Effort.Medium, "low"],
		] as const) {
			const spec = specs.find(model => model.id === id);
			if (!spec) throw new Error("Missing Codex model");
			const model = buildModel({ ...spec, preferWebsockets: false });
			let payload: unknown;
			const fetchFn: typeof fetch = Object.assign(
				async (_input: string | URL | Request, init?: RequestInit) => {
					const body = init?.body;
					const decoded =
						typeof body === "string"
							? body
							: body instanceof Uint8Array
								? new TextDecoder().decode(Bun.zstdDecompressSync(body))
								: undefined;
					if (decoded === undefined) throw new Error("Expected request body");
					payload = JSON.parse(decoded);
					const events = [
						{ type: "response.content_part.added", part: { type: "output_text", text: "" } },
						{ type: "response.output_text.delta", delta: "hello" },
						{
							type: "response.output_item.done",
							item: {
								type: "message",
								id: "msg_1",
								role: "assistant",
								status: "completed",
								content: [{ type: "output_text", text: "hello" }],
							},
						},
						{
							type: "response.completed",
							response: { status: "completed", usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } },
						},
					];
					return new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""), {
						headers: { "content-type": "text/event-stream" },
					});
				},
				{ preconnect() {} },
			);
			const result = await streamSimple(
				model,
				{ messages: [{ role: "user", content: "hello", timestamp: 0 }] },
				{
					apiKey: `aaa.${tokenPayload}.bbb`,
					reasoning: requested,
					fetch: fetchFn,
					preferWebsockets: false,
				},
			).result();
			expect(result.stopReason).toBe("stop");
			expect(payload).toMatchObject({ reasoning: { effort: expected } });
		}
	});

	it("uses the account's actual ladder and defaults, overriding bundled model rules", async () => {
		const specs = await codexSpecs();
		const requiredSpec = specs.find(spec => spec.id === "gpt-6.1-sol");
		const optionalSpec = specs.find(spec => spec.id === "gpt-5.3-codex");
		const disabledSpec = specs.find(spec => spec.id === "gpt-5.6-sol");
		if (!requiredSpec || !optionalSpec || !disabledSpec) throw new Error("Missing API models");
		const mandatory = buildModel(requiredSpec);
		const optional = buildModel(optionalSpec);
		const staleCompat = buildModel({
			...optionalSpec,
			compat: { supportsReasoningEffort: false, omitReasoningEffort: true },
		});
		expect(staleCompat.compat.supportsReasoningEffort).toBe(true);
		expect(staleCompat.compat.omitReasoningEffort).toBe(false);
		expect(getSupportedEfforts(mandatory)).toEqual([Effort.Low, Effort.High]);
		expect(getSupportedEfforts(optional)).toEqual([Effort.Minimal, Effort.Max]);
		expect(mandatory.thinking?.defaultLevel).toBe(Effort.High);
		expect(optional.thinking?.requiresEffort).toBe(false);
		for (const [model, requested, expected] of [
			[mandatory, "max", "high"],
			[optional, "max", "max"],
			[optional, "minimal", "minimal"],
			[mandatory, "medium", "low"],
		] as const) {
			const body = await transformRequestBody({ model: model.id, input: [] }, model, { reasoningEffort: requested });
			expect(body.reasoning?.effort).toBe(expected);
		}
		expect(resolveThinkingLevelForModel(mandatory, ThinkingLevel.Off)).toBe(ThinkingLevel.High);
		expect(
			(await transformRequestBody({ model: mandatory.id }, mandatory, { reasoningOff: true })).reasoning?.effort,
		).toBe("high");
		expect(
			(await transformRequestBody({ model: optional.id }, optional, { reasoningOff: true })).reasoning?.effort,
		).toBe("none");
		const disabled = buildModel(disabledSpec);
		expect(disabled.reasoning).toBe(false);
		expect(disabled.thinking).toBeUndefined();
		expect(
			(await transformRequestBody({ model: disabled.id }, disabled, { reasoningEffort: "max" })).reasoning,
		).toBeUndefined();
	});

	it("preserves advertised Codex capabilities across static merging and cached startup", async () => {
		const home = await fs.mkdtemp(path.join(os.tmpdir(), "mercury-advertised-codex-"));
		try {
			const specs = await codexSpecs();
			const options = {
				...openaiCodexModelManagerOptions(),
				cacheDbPath: path.join(home, "models.db"),
				staticModels: specs.map(spec => ({
					...spec,
					reasoning: true,
					reasoningCapabilities: undefined,
					thinking: undefined,
				})),
				fetchDynamicModels: async () => specs,
			};
			const online = await resolveProviderModels(options, "online");
			const offline = await resolveProviderModels(
				{
					...options,
					fetchDynamicModels: async () => {
						throw new Error("offline");
					},
				},
				"offline",
			);
			for (const result of [online, offline]) {
				const mandatory = result.models.find(model => model.id === "gpt-6.1-sol");
				const disabled = result.models.find(model => model.id === "gpt-5.6-sol");
				if (!mandatory || !disabled) throw new Error("Missing discovered models");
				expect(getSupportedEfforts(mandatory)).toEqual([Effort.Low, Effort.High]);
				expect(disabled.reasoning).toBe(false);
				expect(disabled.thinking).toBeUndefined();
			}
		} finally {
			await fs.rm(home, { recursive: true, force: true });
		}
	});

	for (const [label, reasoning, expected] of [
		[
			"restricted",
			{ supported_efforts: ["high", "low"], mandatory: true, default_effort: "high" },
			[Effort.Low, Effort.High],
		],
		["omitted", { mandatory: false }, []],
		["empty", { supported_efforts: [] }, []],
		["null", { supported_efforts: null }, [...THINKING_EFFORTS]],
	] as const) {
		it(`respects OpenRouter's ${label} effort selector after merging and cache reload`, async () => {
			const home = await fs.mkdtemp(path.join(os.tmpdir(), "mercury-advertised-openrouter-"));
			try {
				const options = openrouterModelManagerOptions({
					fetch: catalogFetch({
						data: [
							{
								id: "xiaomi/mimo-v2.6-pro",
								supported_parameters: ["tools", "reasoning"],
								reasoning,
							},
						],
					}),
				});
				const specs = await options.fetchDynamicModels?.();
				const spec = specs?.[0];
				if (!spec) throw new Error("Missing OpenRouter model");
				const manager = {
					...options,
					cacheDbPath: path.join(home, "models.db"),
					staticModels: [{ ...spec, thinking: undefined, reasoningCapabilities: undefined }],
				};
				const online = await resolveProviderModels(manager, "online");
				const offline = await resolveProviderModels(
					{
						...manager,
						fetchDynamicModels: async () => {
							throw new Error("offline");
						},
					},
					"offline",
				);
				for (const result of [online, offline]) {
					const model = result.models[0];
					expect(model.reasoning).toBe(true);
					expect(getSupportedEfforts(model)).toEqual(expected);
					expect(clampThinkingLevelForModel(model, Effort.Max)).toBe(expected.at(-1));
					const params: OpenAICompletionsParams = { messages: [], model: model.id, stream: true };
					applyChatCompletionsCompatPolicy(
						params,
						resolveOpenAICompatPolicy(model, {
							endpoint: "chat-completions",
							reasoning: Effort.Max,
						}),
					);
					if (expected.length > 0) expect(params).toMatchObject({ reasoning: { effort: expected.at(-1) } });
					else expect(params).not.toHaveProperty("reasoning.effort");
					const responseParams = { model: model.id, stream: true as const };
					applyResponsesCompatPolicy(
						responseParams,
						resolveOpenAICompatPolicy(model, {
							endpoint: "responses",
							reasoning: Effort.Max,
						}),
						undefined,
					);
					if (expected.length > 0)
						expect(responseParams).toMatchObject({ reasoning: { effort: expected.at(-1) } });
					else expect(responseParams).not.toHaveProperty("reasoning.effort");
				}
			} finally {
				await fs.rm(home, { recursive: true, force: true });
			}
		});
	}
});
