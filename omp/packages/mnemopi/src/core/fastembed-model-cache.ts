import * as fs from "node:fs/promises";
import * as path from "node:path";

/** Validate provisioned assets without extracting archives or retrieving anything remotely. */
export async function requireFastembedModelAssets(
	model: string,
	cacheDir: string,
): Promise<{
	modelAbsoluteDirPath: string;
	modelName: string;
}> {
	const modelAbsoluteDirPath = path.resolve(cacheDir, model);
	const modelName =
		model === "fast-multilingual-e5-large" || model === "fast-all-MiniLM-L6-v2"
			? "model.onnx"
			: "model_optimized.onnx";
	for (const name of [
		modelName,
		"config.json",
		"tokenizer.json",
		"tokenizer_config.json",
		"special_tokens_map.json",
	]) {
		const file = path.join(modelAbsoluteDirPath, name);
		const present = await fs
			.stat(file)
			.then(stat => stat.isFile() && stat.size > 0)
			.catch(() => false);
		if (!present) {
			throw new Error(
				`Local embedding assets missing: ${file}. Provision the complete model directory locally (including ONNX external data), or configure an embedding provider. Mercury does not download model weights or sidecars.`,
			);
		}
	}
	return { modelAbsoluteDirPath, modelName };
}
