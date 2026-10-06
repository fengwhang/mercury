import { afterEach, beforeEach, expect, it, spyOn } from "bun:test";
import * as fs from "node:fs/promises";
import { createRequire } from "node:module";
import * as os from "node:os";
import * as path from "node:path";
import type * as TransformersEnvironmentModule from "../../../node_modules/@huggingface/transformers/types/env";
import type * as TransformersHub from "../../../node_modules/@huggingface/transformers/types/utils/hub";
import { getAgentDir, setAgentDir } from "@oh-my-pi/pi-utils";
import * as utils from "@oh-my-pi/pi-utils";
import * as runtime from "../src/subprocess/worker-runtime";
import { startTinyTitleWorker } from "../src/tiny/worker";
import { startSttWorker } from "../src/stt/asr-worker";
import { getTinyLocalModelSpec } from "../src/tiny/models";
import { getTtsLocalModelSpec } from "../src/tts/models";
import { downloadSttModel } from "../src/stt/downloader";
import { downloadTtsModel } from "../src/tts/downloader";
import { sttClient } from "../src/stt/asr-client";
import { ttsClient } from "../src/tts/tts-client";
import { startTtsWorker } from "../src/tts/tts-worker";

// Load the pinned implementation's actual resolver, not a fake hub. Using its
// runtime-resolved package root also works in isolated workspace installations.
const requireBackend = createRequire(import.meta.url);
const backendRoot = path.dirname(path.dirname(requireBackend.resolve("@huggingface/transformers")));
const environmentModule = requireBackend(path.join(backendRoot, "src/env.js")) as typeof TransformersEnvironmentModule;
const hubModule = requireBackend(path.join(backendRoot, "src/utils/hub.js")) as typeof TransformersHub;
const transformersEnv = environmentModule.env;
const getModelFile = hubModule.getModelFile;

let previous: string;
let home: string;
beforeEach(async () => {
	previous = getAgentDir();
	home = await fs.mkdtemp(path.join(os.tmpdir(), "omp-offline-assets-"));
	setAgentDir(home);
});
afterEach(async () => {
	setAgentDir(previous);
	await fs.rm(home, { recursive: true, force: true });
});

function request(start: (transport: never) => void, message: object): Promise<{ type: string; error?: string }> {
	const { promise, resolve } = Promise.withResolvers<{ type: string; error?: string }>();
	start({
		onMessage(handler: (message: object) => void) {
			handler(message);
		},
		send(result: { type: string; error?: string }) {
			if (["error", "downloaded", "title", "completion", "audio", "transcript"].includes(result.type))
				resolve(result);
		},
	} as never);
	return promise;
}

for (const [name, start, message] of [
	[
		"tiny completion",
		startTinyTitleWorker,
		{ type: "complete", id: "tiny", modelKey: "lfm2.5-350m", prompt: "hello" },
	],
	["Whisper STT", startSttWorker, { type: "download", id: "stt", modelKey: "fast" }],
	["Sherpa STT", startSttWorker, { type: "download", id: "sherpa", modelKey: "parakeet" }],
	["Kokoro TTS", startTtsWorker, { type: "synthesize", id: "tts", modelKey: "kokoro", text: "hello" }],
] as const) {
	it(`${name} refuses missing assets without backend initialization or network`, async () => {
		const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
		const backend = spyOn(runtime, "loadTransformersRuntime").mockRejectedValue(new Error("backend must not load"));
		const install = spyOn(utils, "ensureRuntimeInstalled").mockRejectedValue(
			new Error("runtime install must not run"),
		);
		try {
			const result = await request(start, message);
			expect(result.type).toBe("error");
			expect(result.error?.split("\n")[0]).toMatch(/local.*assets/i);
			expect(network).not.toHaveBeenCalled();
			expect(backend).not.toHaveBeenCalled();
			expect(install).not.toHaveBeenCalled();
		} finally {
			network.mockRestore();
			backend.mockRestore();
			install.mockRestore();
		}
	});
}

it("tiny worker loads a provisioned fixture with the local-only backend option", async () => {
	const { getTinyModelsCacheDir } = utils;
	const repo = getTinyLocalModelSpec("lfm2.5-350m")!.repo;
	for (const name of ["config.json", "tokenizer.json", "onnx/model.onnx"])
		await Bun.write(path.join(getTinyModelsCacheDir(), repo, name), "fixture");
	let calls = 0;
	const backend = spyOn(runtime, "loadTransformersRuntime").mockResolvedValue({
		pipeline: async (_task: string, model: string, options: { local_files_only?: boolean }) => {
			calls++;
			expect(model).toBe(repo);
			expect(options.local_files_only).toBe(true);
			return {};
		},
	} as never);
	const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
	try {
		const result = await request(startTinyTitleWorker, { type: "download", id: "fixture", modelKey: "lfm2.5-350m" });
		expect(result.type).toBe("downloaded");
		expect(calls).toBe(1);
		expect(network).not.toHaveBeenCalled();
	} finally {
		backend.mockRestore();
		network.mockRestore();
	}
});

it("Transformers' actual asset resolver cannot fetch missing optional or required files", async () => {
	const env = transformersEnv;
	const saved = {
		cacheDir: env.cacheDir,
		localModelPath: env.localModelPath,
		allowLocalModels: env.allowLocalModels,
		allowRemoteModels: env.allowRemoteModels,
		logLevel: env.logLevel,
	};
	const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
	try {
		const localEnv = Object.assign(env, { cacheDir: utils.getTinyModelsCacheDir() });
		runtime.configureTransformers({ env: localEnv, LogLevel: environmentModule.LogLevel });
		await expect(getModelFile("fixture/missing", "config.json", true, { local_files_only: true })).rejects.toThrow(
			"file was not found locally",
		);
		expect(await getModelFile("fixture/missing", "optional.json", false, { local_files_only: true })).toBeNull();
		// Kokoro 1.2.1 drops local_files_only when forwarding to Transformers.
		await expect(getModelFile("fixture/missing", "model.onnx", true, {})).rejects.toThrow(
			"file was not found locally",
		);
		expect(env.allowRemoteModels).toBe(false);
		expect(network).not.toHaveBeenCalled();
	} finally {
		Object.assign(env, saved);
		network.mockRestore();
	}
});

it("speech preparation refuses missing local assets without starting worker clients", async () => {
	const stt = spyOn(sttClient, "downloadModel").mockRejectedValue(new Error("must not call worker"));
	const tts = spyOn(ttsClient, "downloadModel").mockRejectedValue(new Error("must not call worker"));
	try {
		await expect(downloadSttModel("fast")).rejects.toThrow("Local model assets missing");
		await expect(downloadSttModel("parakeet")).rejects.toThrow("Local STT assets missing");
		await expect(downloadTtsModel("kokoro")).rejects.toThrow("Local model assets missing");
		expect(stt).not.toHaveBeenCalled();
		expect(tts).not.toHaveBeenCalled();
	} finally {
		stt.mockRestore();
		tts.mockRestore();
	}
});

it("Kokoro worker configures its actual runtime boundary local-only before synthesis", async () => {
	const spec = getTtsLocalModelSpec("kokoro")!;
	for (const name of ["config.json", "tokenizer.json", "onnx/model.onnx"])
		await Bun.write(path.join(utils.getTinyModelsCacheDir(), spec.repo, name), "fixture");
	const runtimeDir = path.join(home, "runtime");
	const nodeModules = path.join(runtimeDir, "node_modules");
	const transformersDir = path.join(nodeModules, "@huggingface", "transformers");
	const kokoroDir = path.join(nodeModules, "kokoro-js");
	await Bun.write(path.join(transformersDir, "package.json"), JSON.stringify({ main: "index.cjs" }));
	await Bun.write(
		path.join(transformersDir, "index.cjs"),
		'module.exports = { env: { allowRemoteModels: true, allowLocalModels: false }, LogLevel: { ERROR: "error" } };',
	);
	await Bun.write(path.join(kokoroDir, "package.json"), JSON.stringify({ main: "dist/index.cjs" }));
	for (const voice of spec.voices) await Bun.write(path.join(kokoroDir, "voices", `${voice.id}.bin`), "fixture");
	await Bun.write(
		path.join(kokoroDir, "dist/index.cjs"),
		`
		const { env } = require("../../@huggingface/transformers/index.cjs");
		module.exports.KokoroTTS = { from_pretrained: async (repo, options) => {
			if (env.allowRemoteModels !== false || env.allowLocalModels !== true || !env.localModelPath || options.local_files_only !== true) throw new Error("unsafe model loader");
			return { generate: async () => ({ audio: new Float32Array([0.5]), sampling_rate: 24000 }) };
		} };
	`,
	);
	const install = spyOn(utils, "ensureRuntimeInstalled").mockResolvedValue(runtimeDir);
	const resolver = spyOn(runtime, "installSharpStubResolver").mockResolvedValue(nodeModules);
	const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
	try {
		const result = await request(startTtsWorker, {
			type: "synthesize",
			id: "fixture-tts",
			modelKey: "kokoro",
			text: "hello",
		});
		expect(result.type).toBe("audio");
		expect(network).not.toHaveBeenCalled();
	} finally {
		install.mockRestore();
		resolver.mockRestore();
		network.mockRestore();
	}
});
