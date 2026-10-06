import { expect, spyOn, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { defaultLocalModelInitializer } from "../src/core/embeddings";
import * as runtime from "../src/core/fastembed-runtime";

test("a corrupt provisioned model is quarantined once without entering retrieval", async () => {
	const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), "mnemopi-corrupt-"));
	const model = "fast-bge-small-en-v1.5";
	const dir = path.join(cacheDir, model);
	for (const name of [
		"model_optimized.onnx",
		"config.json",
		"tokenizer.json",
		"tokenizer_config.json",
		"special_tokens_map.json",
	])
		await Bun.write(path.join(dir, name), "corrupt");
	let calls = 0;
	const backend = spyOn(runtime, "loadFastembed").mockResolvedValue({
		EmbeddingModel: { CUSTOM: "custom" },
		FlagEmbedding: {
			init: async () => {
				calls++;
				throw new Error("Protobuf parsing failed");
			},
		},
	} as never);
	const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("must not fetch"));
	try {
		await expect(defaultLocalModelInitializer({ model: model as never, cacheDir })).rejects.toThrow(
			"Local embedding assets/backend could not load",
		);
		expect(calls).toBe(1);
		expect(await Bun.file(path.join(dir, "model_optimized.onnx")).exists()).toBe(false);
		const quarantined = (await fs.readdir(dir)).filter(name => name.startsWith("model_optimized.onnx.corrupt-"));
		expect(quarantined).toHaveLength(1);
		expect(await Bun.file(path.join(dir, quarantined[0]!)).text()).toBe("corrupt");
		expect(network).not.toHaveBeenCalled();
	} finally {
		backend.mockRestore();
		network.mockRestore();
		await fs.rm(cacheDir, { recursive: true, force: true });
	}
});
