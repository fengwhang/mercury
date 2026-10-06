import * as fs from "node:fs/promises";
import * as path from "node:path";
import { getTinyModelsCacheDir } from "@oh-my-pi/pi-utils";
import { sttClient } from "./asr-client";
import type { SttProgressStatus } from "./asr-protocol";
import { requireLocalModelAssets } from "../subprocess/worker-runtime";
import { resolveSttModelSpec } from "./models";

export interface DownloadProgress {
	stage: string;
	percent?: number;
}

export interface EnsureOptions {
	modelName?: string;
	signal?: AbortSignal;
	onProgress?: (progress: DownloadProgress) => void;
}

// ── ONNX Whisper model ─────────────────────────────────────────────

/**
 * Real-progress event for a speech-model download, surfaced to UI callers.
 * `percent` is an integer 0–100 aggregated across all model files (encoder +
 * decoder shards), so it advances monotonically toward completion.
 */
export interface SttDownloadProgress {
	status: SttProgressStatus;
	/** Integer 0–100 aggregated across files. */
	percent: number;
	/** Bytes downloaded so far across all files. */
	loaded: number;
	/** Total bytes across all files seen so far. */
	total: number;
	/** The file currently downloading, when known. */
	file?: string;
	repo: string;
	label: string;
}

/**
 * Whether the selected model is fully present in the local cache. For
 * transformers.js Whisper tiers a complete download leaves `config.json` plus
 * matching `encoder*.onnx` and `decoder*.onnx` shards under `onnx/` (a partial
 * fetch with only one shard, or a bare `config.json`, reads as not-cached); for
 * sherpa-onnx tiers every model file (encoder/decoder/joiner + tokens) must be
 * present (`.part` sidecars from an interrupted fetch are ignored).
 */
export async function isSttModelCached(key: string): Promise<boolean> {
	const spec = resolveSttModelSpec(key);
	const repoDir = path.join(getTinyModelsCacheDir(), spec.repo);
	if (spec.engine === "sherpa") {
		try {
			for (const role in spec.files) {
				const file = path.join(repoDir, spec.files[role as keyof typeof spec.files]);
				const present = await fs
					.stat(file)
					.then(stat => stat.isFile() && stat.size > 0)
					.catch(() => false);
				if (!present) return false;
			}
			return true;
		} catch {
			return false;
		}
	}
	try {
		await requireLocalModelAssets(spec.repo);
		// Whisper is encoder-decoder: require both graph shards, not just one
		// weight file. The worker enforces local-only for dtype variants and data.
		const onnxFiles = await fs.readdir(path.join(repoDir, "onnx")).catch(() => [] as string[]);
		const hasEncoder = onnxFiles.some(file => file.startsWith("encoder") && file.endsWith(".onnx"));
		const hasDecoder = onnxFiles.some(file => file.startsWith("decoder") && file.endsWith(".onnx"));
		return hasEncoder && hasDecoder;
	} catch {
		return false;
	}
}

/**
 * Warm a provisioned local speech model through the worker. Missing files fail
 * locally; Mercury never downloads weights. Progress describes runtime preparation
 * and model readiness rather than remote transfers.
 */
export async function downloadSttModel(
	key: string,
	onProgress?: (progress: SttDownloadProgress) => void,
	options?: { signal?: AbortSignal },
): Promise<void> {
	const spec = resolveSttModelSpec(key);
	if (spec.engine === "transformers") await requireLocalModelAssets(spec.repo);
	else if (!(await isSttModelCached(spec.key)))
		throw new Error(
			`Local STT assets missing: ${path.join(getTinyModelsCacheDir(), spec.repo)}. Provision all encoder/decoder/joiner/tokens files locally or configure an STT provider. Model downloads are disabled.`,
		);
	const files = new Map<string, { loaded: number; total: number }>();
	const result = await sttClient.downloadModel(spec.key, {
		signal: options?.signal,
		onProgress: event => {
			if ((event.status === "progress" || event.status === "progress_total") && event.file) {
				if (typeof event.loaded === "number" && typeof event.total === "number" && event.total > 0) {
					files.set(event.file, { loaded: event.loaded, total: event.total });
				}
			}
			let loaded = 0;
			let total = 0;
			for (const file of files.values()) {
				loaded += file.loaded;
				total += file.total;
			}
			const settled = event.status === "ready" || event.status === "done";
			const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : settled ? 100 : 0;
			onProgress?.({
				status: event.status,
				percent,
				loaded,
				total,
				file: event.file,
				repo: spec.repo,
				label: spec.label,
			});
		},
	});
	if (!result.ok) {
		const detail = result.error ? `: ${result.error}` : ". Configure an STT provider or repair local assets/backend.";
		throw new Error(`Failed to load local speech model (${spec.repo})${detail}`);
	}
	if (!(await isSttModelCached(spec.key))) {
		throw new Error(`Local speech model is missing required files (${spec.repo}).`);
	}
}

// ── Public API ─────────────────────────────────────────────────────

export async function ensureSTTDependencies(options?: EnsureOptions): Promise<void> {
	await downloadSttModel(
		resolveSttModelSpec(options?.modelName).key,
		progress => {
			const stage =
				progress.status === "ready" || progress.status === "done"
					? `Speech model ${progress.label} ready`
					: `Preparing local speech model ${progress.label}`;
			options?.onProgress?.({ stage, percent: progress.percent });
		},
		{ signal: options?.signal },
	);
}
