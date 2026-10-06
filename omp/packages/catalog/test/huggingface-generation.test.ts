import { describe, expect, test } from "bun:test";
import { fetchProviderModelsFromCatalog } from "../scripts/generate-models";
import { isCatalogDescriptor } from "../src/provider-models/descriptor-types";
import { PROVIDER_DESCRIPTORS } from "../src/provider-models/descriptors";
import type { FetchImpl } from "../src/types";

const descriptor = PROVIDER_DESCRIPTORS.find(entry => entry.providerId === "huggingface")!;
if (!isCatalogDescriptor(descriptor)) throw new Error("Missing Hugging Face catalog descriptor");

describe("gen:models Hugging Face refresh seam (offline)", () => {
	for (const baseUrl of [undefined, "https://router.huggingface.co/v1"]) {
		test(`refuses ${baseUrl ?? "credential-only configuration"} visibly before HTTP`, async () => {
			let requests = 0;
			const fetch: FetchImpl = async () => {
				requests++;
				throw new Error("unexpected HTTP");
			};
			const result = await fetchProviderModelsFromCatalog(descriptor, { apiKey: "test-key", baseUrl, fetch });
			expect(result).toEqual({ models: [], succeeded: false });
			expect(requests).toBe(0);
		});
	}

	test("passes existing explicit discovery baseUrl and fetch through the actual manager refresh", async () => {
		const urls: string[] = [];
		const fetch: FetchImpl = async input => {
			urls.push(String(input));
			return Response.json({ data: [{ id: "local-model" }] });
		};
		const result = await fetchProviderModelsFromCatalog(descriptor, {
			apiKey: "test-key",
			baseUrl: "https://example.invalid/v1",
			fetch,
		});
		expect(result.succeeded).toBe(true);
		expect(urls).toEqual(["https://example.invalid/v1/models"]);
		expect(result.models.find(model => model.id === "local-model")?.baseUrl).toBe("https://example.invalid/v1");
	});
});
