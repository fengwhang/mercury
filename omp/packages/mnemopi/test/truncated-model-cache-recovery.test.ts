import { describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { defaultLocalModelInitializer } from "../src/core/embeddings";
import * as runtime from "../src/core/fastembed-runtime";

const MODEL = "fast-multilingual-e5-large";
describe("provisioned fastembed assets", () => {
	test("incomplete caches and archives are preserved and never retried or downloaded", async () => {
		const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), "mnemopi-partial-"));
		const modelDir = path.join(cacheDir, MODEL);
		await Bun.write(path.join(modelDir, "model.onnx_data"), "partial");
		await Bun.write(`${modelDir}.tar.gz`, "partial archive");
		const backend = spyOn(runtime, "loadFastembed").mockRejectedValue(new Error("must not initialize"));
		try {
			await expect(defaultLocalModelInitializer({ model: MODEL as never, cacheDir })).rejects.toThrow(
				"Local embedding assets missing",
			);
			expect(backend).not.toHaveBeenCalled();
			expect(await Bun.file(`${modelDir}.tar.gz`).text()).toBe("partial archive");
			expect(await Bun.file(path.join(modelDir, "model.onnx_data")).text()).toBe("partial");
		} finally {
			backend.mockRestore();
			await fs.rm(cacheDir, { recursive: true, force: true });
		}
	});
	test("complete local assets use the no-retrieval branch and preserve e5 inference identity", async () => {
		const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), "mnemopi-provisioned-"));
		const dir = path.join(cacheDir, MODEL);
		for (const name of [
			"model.onnx",
			"config.json",
			"tokenizer.json",
			"tokenizer_config.json",
			"special_tokens_map.json",
		])
			await Bun.write(path.join(dir, name), "fixture");
		const model = {
			model: "custom",
			embed: async function* () {
				yield [[1, 2]];
			},
		};
		const init = async (options: { model: string; modelName: string; modelAbsoluteDirPath: string }) => {
			expect(options.model).toBe("custom");
			expect(options.modelName).toBe("model.onnx");
			expect(options.modelAbsoluteDirPath).toBe(dir);
			return model;
		};
		const backend = spyOn(runtime, "loadFastembed").mockResolvedValue({
			FlagEmbedding: { init },
			EmbeddingModel: { CUSTOM: "custom" },
		} as never);
		const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("must not fetch"));
		try {
			const loaded = await defaultLocalModelInitializer({ model: MODEL as never, cacheDir });
			expect(loaded).toBe(model);
			expect(model.model).toBe(MODEL);
			for await (const batch of loaded.embed(["hello"])) expect(batch).toEqual([[1, 2]]);
			expect(network).not.toHaveBeenCalled();
		} finally {
			backend.mockRestore();
			network.mockRestore();
			await fs.rm(cacheDir, { recursive: true, force: true });
		}
	});
});
