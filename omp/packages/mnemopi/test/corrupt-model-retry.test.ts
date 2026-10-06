// Corruption is isolated locally; quarantining assets must never trigger SDK retrieval or a retry.
import { describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import * as utils from "@oh-my-pi/pi-utils";
import { defaultLocalModelInitializer } from "../src/core/embeddings";
import * as runtime from "../src/core/fastembed-runtime";

const MODEL = "fast-bge-small-en-v1.5";
const SIDECARS = ["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"];

async function provisionedCache(): Promise<{ cacheDir: string; modelFile: string }> {
	const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), "mnemopi-corruption-"));
	const modelDir = path.join(cacheDir, MODEL);
	await fs.mkdir(modelDir, { recursive: true });
	const modelFile = path.join(modelDir, "model_optimized.onnx");
	await fs.writeFile(modelFile, "garbage");
	for (const name of SIDECARS) await fs.writeFile(path.join(modelDir, name), "{}");
	return { cacheDir, modelFile };
}

function failingBackend(error: Error) {
	let calls = 0;
	const loadSpy = spyOn(runtime, "loadFastembed").mockResolvedValue({
		EmbeddingModel: { CUSTOM: "custom" },
		FlagEmbedding: {
			init: async () => {
				calls++;
				throw error;
			},
		},
	} as never);
	return { loadSpy, calls: () => calls };
}

describe("defaultLocalModelInitializer local corruption", () => {
	test("protobuf failure quarantines only the admitted ONNX and preserves the original cause without retrying", async () => {
		const { cacheDir, modelFile } = await provisionedCache();
		const otherModelFile = path.join(cacheDir, "other-model", "model_optimized.onnx");
		await Bun.write(otherModelFile, "unrelated");
		const error = new Error(`Load model from ${otherModelFile} failed:Protobuf parsing failed.`);
		const backend = failingBackend(error);
		const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
		try {
			const failure = await defaultLocalModelInitializer({ model: MODEL as never, cacheDir }).catch(error => error);
			expect(failure).toBeInstanceOf(Error);
			expect(failure.message).toContain("Repair the local model");
			expect(failure.message).toContain("No download was attempted");
			expect(failure.cause).toBe(error);
			expect(backend.calls()).toBe(1);
			const siblings = await fs.readdir(path.dirname(modelFile));
			const quarantined = siblings.filter(name => name.startsWith("model_optimized.onnx.corrupt-"));
			expect(quarantined).toHaveLength(1);
			expect(await Bun.file(path.join(path.dirname(modelFile), quarantined[0]!)).text()).toBe("garbage");
			expect(await Bun.file(modelFile).exists()).toBe(false);
			for (const name of SIDECARS)
				expect(await Bun.file(path.join(path.dirname(modelFile), name)).text()).toBe("{}");
			expect(await Bun.file(otherModelFile).text()).toBe("unrelated");
			expect(network).not.toHaveBeenCalled();
		} finally {
			backend.loadSpy.mockRestore();
			network.mockRestore();
			await fs.rm(cacheDir, { recursive: true, force: true });
		}
	});

	test("uses the shared default cache root and CUSTOM filesystem loading when cacheDir is omitted", async () => {
		const { cacheDir, modelFile } = await provisionedCache();
		const rootSpy = spyOn(utils, "getFastembedCacheDir").mockReturnValue(cacheDir);
		const model = {
			model: "custom",
			embed: async function* () {
				yield [[1, 2]];
			},
		};
		const initOptions: unknown[] = [];
		const loadSpy = spyOn(runtime, "loadFastembed").mockResolvedValue({
			EmbeddingModel: { CUSTOM: "custom" },
			FlagEmbedding: {
				init: async (options: unknown) => {
					initOptions.push(options);
					return model;
				},
			},
		} as never);
		const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
		try {
			const loaded = await defaultLocalModelInitializer({ model: MODEL as never });
			expect(rootSpy).toHaveBeenCalledTimes(1);
			expect(loaded).toBe(model);
			expect(initOptions).toEqual([
				{
					model: "custom",
					modelAbsoluteDirPath: path.dirname(modelFile),
					modelName: "model_optimized.onnx",
					showDownloadProgress: false,
				},
			]);
			expect(model.model).toBe(MODEL);
			for await (const batch of loaded.embed(["hello"])) expect(batch).toEqual([[1, 2]]);
			expect(await Bun.file(modelFile).text()).toBe("garbage");
			expect(network).not.toHaveBeenCalled();
		} finally {
			rootSpy.mockRestore();
			loadSpy.mockRestore();
			network.mockRestore();
			await fs.rm(cacheDir, { recursive: true, force: true });
		}
	});

	test("a second attempt after quarantine fails admission before the SDK or network", async () => {
		const { cacheDir, modelFile } = await provisionedCache();
		const error = new Error(`Load model from ${modelFile} failed:Protobuf parsing failed.`);
		const backend = failingBackend(error);
		const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
		try {
			const failure = await defaultLocalModelInitializer({ model: MODEL as never, cacheDir }).catch(error => error);
			expect(failure.cause).toBe(error);
			await expect(defaultLocalModelInitializer({ model: MODEL as never, cacheDir })).rejects.toThrow(
				"Local embedding assets missing",
			);
			expect(backend.calls()).toBe(1);
			expect(backend.loadSpy).toHaveBeenCalledTimes(1);
			expect(
				(await fs.readdir(path.dirname(modelFile))).filter(name =>
					name.startsWith("model_optimized.onnx.corrupt-"),
				),
			).toHaveLength(1);
			expect(network).not.toHaveBeenCalled();
		} finally {
			backend.loadSpy.mockRestore();
			network.mockRestore();
			await fs.rm(cacheDir, { recursive: true, force: true });
		}
	});

	test("a non-protobuf backend failure preserves assets and its original cause without retrying", async () => {
		const { cacheDir, modelFile } = await provisionedCache();
		const error = new Error("ONNX external data file missing");
		const backend = failingBackend(error);
		const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
		try {
			const failure = await defaultLocalModelInitializer({ model: MODEL as never, cacheDir }).catch(error => error);
			expect(failure.cause).toBe(error);
			expect(backend.calls()).toBe(1);
			expect(await Bun.file(modelFile).text()).toBe("garbage");
			expect(await fs.readdir(path.dirname(modelFile))).toHaveLength(SIDECARS.length + 1);
			expect(network).not.toHaveBeenCalled();
		} finally {
			backend.loadSpy.mockRestore();
			network.mockRestore();
			await fs.rm(cacheDir, { recursive: true, force: true });
		}
	});
});
