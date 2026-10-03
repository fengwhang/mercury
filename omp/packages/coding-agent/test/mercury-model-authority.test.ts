import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as os from "node:os";
import { Effort } from "@oh-my-pi/pi-ai";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { TempDir } from "@oh-my-pi/pi-utils";
import { YAML } from "bun";
import { beginSettingsTest, restoreSettingsTestState, type SettingsTestState } from "./helpers/settings-test-state";

interface StoredDocument {
	models: {
		default: string;
		delegate_model: string;
		delegate_fallback: string;
		reasoning_overrides: Record<string, string>;
	};
	hermes: { approvals: { mode: string } };
	omp: { tools: { approvalMode: string }; retry: { fallbackChains?: unknown }; [key: string]: unknown };
}

let state: SettingsTestState;
let temp: TempDir;
let oldConfig: string | undefined;

beforeEach(() => {
	state = beginSettingsTest();
	temp = TempDir.createSync(path.join(os.tmpdir(), "mercury-model-authority-"));
	oldConfig = process.env.MERCURY_CONFIG;
});

afterEach(() => {
	if (oldConfig === undefined) delete process.env.MERCURY_CONFIG;
	else process.env.MERCURY_CONFIG = oldConfig;
	restoreSettingsTestState(state);
	temp.removeSync();
});

test("OMP projects shared choices and its picker saves only the shared delegate model", async () => {
	const file = temp.join("config.yaml");
	process.env.MERCURY_CONFIG = file;
	const primary = "openai-codex/gpt-6.1-sol";
	const fallback = "nous/xiaomi/mimo-v2.6-pro";
	await Bun.write(
		file,
		YAML.stringify({
			models: {
				default: primary,
				delegate_model: primary,
				delegate_fallback: fallback,
				reasoning_overrides: { [primary]: "high", [fallback]: "medium" },
				context_windows: { [primary]: 872000 },
			},
			hermes: { approvals: { mode: "smart" } },
			omp: {
				tools: { approvalMode: "write" },
				modelRoles: { task: "openrouter/old" },
				defaultThinkingLevel: "xhigh",
				retry: { fallbackChains: { "openrouter/old": ["openrouter/stale"] } },
			},
		}),
	);
	await fs.mkdir(temp.join("agent"), { recursive: true });
	const settings = await Settings.init({ agentDir: temp.join("agent"), cwd: temp.path() });
	expect(settings.getModelRole("task")).toBe(primary);
	expect(settings.get("defaultThinkingLevel")).toBe(Effort.High);
	expect(settings.get("retry.fallbackChains")).toEqual({ [primary]: [`${fallback}:medium`] });
	expect(settings.get("modelContextWindows")[primary]).toBe(872000);
	settings.setModelRole("task", `${fallback}:low`);
	await settings.flush();
	const saved = YAML.parse(await Bun.file(file).text()) as StoredDocument;
	expect(saved.models.default).toBe(primary);
	expect(saved.models.delegate_model).toBe(fallback);
	expect(saved.models.reasoning_overrides[fallback]).toBe(Effort.Low);
	expect(saved.omp.modelRoles).toBeUndefined();
	expect(saved.omp.delegateModel).toBeUndefined();
	expect(saved.omp.defaultThinkingLevel).toBeUndefined();
	expect(saved.omp.retry.fallbackChains).toBeUndefined();
	expect(saved.omp.tools.approvalMode).toBe("write");
	expect(saved.hermes.approvals.mode).toBe("smart");
	await settings.reloadFromDisk();
	expect(settings.getModelRole("task")).toBe(fallback);
	expect(settings.get("defaultThinkingLevel")).toBe(Effort.Low);
	settings.setModelReasoning(primary, "off");
	settings.set("delegateFallback", `${primary}:off`);
	await settings.flush();
	const updated = YAML.parse(await Bun.file(file).text()) as StoredDocument;
	expect(updated.models.delegate_model).toBe(fallback);
	expect(updated.models.delegate_fallback).toBe(primary);
	expect(updated.models.reasoning_overrides[primary]).toBe("off");
	expect(updated.models.reasoning_overrides[fallback]).toBe(Effort.Low);
	expect(updated.omp.modelReasoningOverrides).toBeUndefined();
	expect(updated.omp.delegateFallback).toBeUndefined();
});
