import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import { resolveThresholdTokens, shouldCompact } from "@oh-my-pi/pi-agent-core/compaction/compaction";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { fetchCodexModels } from "@oh-my-pi/pi-catalog/discovery/codex";
import { applyContextWindow } from "@oh-my-pi/pi-coding-agent/config/model-context";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { TempDir } from "@oh-my-pi/pi-utils";
import { YAML } from "bun";
import { beginSettingsTest, restoreSettingsTestState, type SettingsTestState } from "./helpers/settings-test-state";

let state: SettingsTestState;
let temp: TempDir;
let oldConfig: string | undefined;

beforeEach(() => {
	state = beginSettingsTest();
	temp = TempDir.createSync("mercury-context-");
	oldConfig = process.env.MERCURY_CONFIG;
});

afterEach(() => {
	if (oldConfig === undefined) delete process.env.MERCURY_CONFIG;
	else process.env.MERCURY_CONFIG = oldConfig;
	restoreSettingsTestState(state);
	temp.removeSync();
});

test("Codex discovery keeps its account maximum and explicit budgets change compaction", async () => {
	const fetchFn: typeof fetch = Object.assign(
		async () =>
			new Response(
				JSON.stringify({
					models: [
						{ slug: "gpt-6.1-sol", context_window: 272000, max_context_window: 872000 },
						{ slug: "gpt-5.6-sol", context_window: 272000, max_context_window: 872000 },
						{ slug: "single-window", context_window: 64000 },
					],
				}),
			),
		{ preconnect() {} },
	);
	const result = await fetchCodexModels({ accessToken: "fixture-token", fetchFn });
	const single = buildModel(result!.models.find(model => model.id === "single-window")!);
	expect(applyContextWindow(single, {}, true).contextWindow).toBe(64000);
	for (const id of ["gpt-6.1-sol", "gpt-5.6-sol"]) {
		const model = buildModel(result!.models.find(model => model.id === id)!);
		expect(model.contextWindow).toBe(272000); // catalogue floors cannot replace account metadata
		const maximum = applyContextWindow(model, {}, true);
		expect(maximum.contextWindow).toBe(872000);
		const selected = applyContextWindow(model, { [`openai-codex/${id}`]: 400000 }, false);
		expect(
			resolveThresholdTokens(selected.contextWindow!, {
				enabled: true,
				thresholdPercent: 75,
				keepRecentTokens: 1000,
			}),
		).toBe(300000);
		expect(applyContextWindow(model, { [`openai-codex/${id}`]: 1050000 }, false).contextWindow).toBe(872000);
	}
});

test("Mercury profile settings override stale native thresholds and budget the active model", async () => {
	const file = temp.join("config.yaml");
	process.env.MERCURY_CONFIG = file;
	await Bun.write(
		file,
		YAML.stringify({
			models: { context_windows: { "openai-codex/gpt-6.1-sol": 872000 } },
			hermes: { compression: { enabled: true, threshold: 0.625 } },
			omp: { compaction: { enabled: false, thresholdPercent: 50, thresholdTokens: 1000 } },
		}),
	);
	await fs.mkdir(temp.join("agent"), { recursive: true });
	const settings = await Settings.init({ agentDir: temp.join("agent"), cwd: temp.path() });
	const thresholdPercent = settings.get("compaction.thresholdPercent");
	const thresholdTokens = settings.get("compaction.thresholdTokens");
	const enabled = settings.get("compaction.enabled");
	const compaction = { enabled, thresholdPercent, thresholdTokens, keepRecentTokens: 1000 };
	expect(resolveThresholdTokens(872000, compaction)).toBe(545000);
	expect(shouldCompact(545001, 872000, compaction)).toBe(true);
	expect(shouldCompact(544999, 872000, compaction)).toBe(false);
	expect(settings.get("modelContextWindows")["openai-codex/gpt-6.1-sol"]).toBe(872000);
});

test("a Mercury profile without an OMP subtree still enables compaction", async () => {
	const file = temp.join("config.yaml");
	process.env.MERCURY_CONFIG = file;
	await Bun.write(file, YAML.stringify({ hermes: {}, models: {} }));
	await fs.mkdir(temp.join("agent"), { recursive: true });
	const settings = await Settings.init({ agentDir: temp.join("agent"), cwd: temp.path() });
	const compaction = {
		enabled: settings.get("compaction.enabled"),
		thresholdPercent: settings.get("compaction.thresholdPercent"),
		keepRecentTokens: 1000,
	};
	expect(shouldCompact(100001, 200000, compaction)).toBe(true);
	expect(shouldCompact(99999, 200000, compaction)).toBe(false);
});
