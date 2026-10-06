import { requireLocalModelAssets } from "../subprocess/worker-runtime";
import { getTtsLocalModelSpec } from "./models";
import { isTtsRuntimeCached } from "./runtime";
import { ttsClient } from "./tts-client";

export interface TtsDownloadProgress {
	stage: string;
	/** Integer 0–100 download percent when known. */
	percent?: number;
}

/**
 * Whether provisioned model assets and the side Kokoro runtime are present.
 * Requires config/tokenizer plus ONNX weights and the versioned runtime.
 */
export async function isTtsModelCached(modelKey: string): Promise<boolean> {
	const spec = getTtsLocalModelSpec(modelKey);
	if (!spec) return false;
	try {
		await requireLocalModelAssets(spec.repo);
		return await isTtsRuntimeCached();
	} catch {
		return false;
	}
}

/**
 * Warm a provisioned local TTS model. Missing assets fail before starting the
 * worker; runtime preparation may install dependencies, never model weights.
 * Returns false if the worker is unavailable or local initialization fails.
 */
export async function downloadTtsModel(
	modelKey: string,
	onProgress?: (progress: TtsDownloadProgress) => void,
	signal?: AbortSignal,
): Promise<boolean> {
	const spec = getTtsLocalModelSpec(modelKey);
	if (!spec) return false;
	await requireLocalModelAssets(spec.repo);
	onProgress?.({ stage: `Preparing ${spec.label}...` });
	return ttsClient.downloadModel(spec.key, {
		signal,
		onProgress: event => {
			if (event.status === "ready" || event.status === "done") {
				onProgress?.({ stage: `${spec.label} ready`, percent: 100 });
				return;
			}
			const percent =
				typeof event.total === "number" && event.total > 0 && typeof event.loaded === "number"
					? Math.round((event.loaded / event.total) * 100)
					: typeof event.progress === "number"
						? Math.round(event.progress)
						: undefined;
			onProgress?.({ stage: `Preparing local ${spec.label}`, percent });
		},
	});
}
