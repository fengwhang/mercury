import { describe, expect, test } from "bun:test";
import { mapModelsDevToModels, MODELS_DEV_PROVIDER_DESCRIPTORS } from "@oh-my-pi/pi-catalog/provider-models";
import { CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL } from "@oh-my-pi/pi-catalog/wire/cloudflare-ai-gateway";

describe("Cloudflare AI Gateway shared catalog", () => {
	test("mirrors active Workers AI chat models into the gateway provider", () => {
		const models = mapModelsDevToModels(
			{
				"cloudflare-workers-ai": {
					models: {
						"@cf/zai-org/glm-5.3-flash": {
							name: "GLM 5.3 Flash",
							tool_call: true,
							reasoning: true,
							modalities: { input: ["text", "image"] },
						},
						"@cf/example/deprecated": {
							name: "Deprecated",
							tool_call: true,
							status: "deprecated",
						},
						"@cf/example/no-tools": {
							name: "No tools",
							tool_call: false,
						},
					},
				},
			},
			MODELS_DEV_PROVIDER_DESCRIPTORS,
		).filter(model => model.provider === "cloudflare-ai-gateway");

		expect(models).toHaveLength(1);
		expect(models[0]).toMatchObject({
			provider: "cloudflare-ai-gateway",
			id: "workers-ai/@cf/zai-org/glm-5.3-flash",
			api: "openai-completions",
			baseUrl: CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL,
			reasoning: true,
			input: ["text", "image"],
		});
	});
});
