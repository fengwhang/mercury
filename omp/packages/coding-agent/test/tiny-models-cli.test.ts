import { afterEach, describe, expect, it, spyOn, vi } from "bun:test";
import { resolveModels, runTinyModelsCommand } from "../src/cli/tiny-models-cli";
import { TINY_LOCAL_MODELS } from "../src/tiny/models";
import { tinyTitleClient } from "../src/tiny/title-client";

afterEach(() => vi.restoreAllMocks());
describe("tiny-models local-only command", () => {
	it("excludes load-blocked models from all", () => {
		const all = resolveModels("all");
		for (const spec of TINY_LOCAL_MODELS) {
			if ("unsupportedReason" in spec && spec.unsupportedReason) expect(all).not.toContain(spec.key);
			else expect(all).toContain(spec.key);
		}
	});
	it("resolves an explicitly requested unsupported model", () => {
		const blocked = TINY_LOCAL_MODELS.find(spec => "unsupportedReason" in spec && spec.unsupportedReason)!;
		expect(resolveModels(blocked.key)).toEqual([blocked.key]);
	});
	it("refuses downloads with honest JSON failure and no worker call", async () => {
		const output: string[] = [];
		spyOn(process.stdout, "write").mockImplementation(chunk => {
			output.push(String(chunk));
			return true;
		});
		const download = spyOn(tinyTitleClient, "downloadModel").mockRejectedValue(new Error("must not start worker"));
		await expect(
			runTinyModelsCommand({ action: "download", model: "lfm2.5-350m", flags: { json: true } }),
		).rejects.toThrow("Model downloads are disabled");
		const result = JSON.parse(output.join(""));
		expect(result.results[0].ok).toBe(false);
		expect(result.results[0].error).toContain("Provision complete local assets");
		expect(download).not.toHaveBeenCalled();
	});
	it("refuses text-mode downloads instead of reporting success", async () => {
		const download = spyOn(tinyTitleClient, "downloadModel").mockRejectedValue(new Error("must not start worker"));
		await expect(runTinyModelsCommand({ action: "download", flags: {} })).rejects.toThrow(
			"Model downloads are disabled",
		);
		expect(download).not.toHaveBeenCalled();
	});
});
