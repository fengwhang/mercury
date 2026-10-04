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

test("a named profile inherits live model defaults without freezing them on native saves", async () => {
	process.env.MERCURY_HOME = temp.path();
	const profile = temp.join("hermes/profiles/research");
	const file = path.join(profile, "config.yaml");
	process.env.MERCURY_CONFIG = file;
	const main = {
		models: {
			default: "openai-codex/chat",
			fallback: "",
			delegate_model: "openai-codex/gpt-6.1-sol",
			delegate_fallback: "nous/xiaomi/mimo-v2.6-pro",
			reasoning_overrides: { "openai-codex/gpt-6.1-sol": "high" },
			context_windows: { "openai-codex/gpt-6.1-sol": 872000 },
		},
	};
	await Bun.write(temp.join("config.yaml"), YAML.stringify(main));
	await Bun.write(file, YAML.stringify({ models: {}, hermes: {}, omp: { tools: { approvalMode: "write" } } }));
	await fs.mkdir(path.join(profile, "omp"), { recursive: true });
	const settings = await Settings.init({ agentDir: path.join(profile, "omp"), cwd: temp.path() });
	expect(settings.getModelRole("task")).toBe(main.models.delegate_model);
	expect(settings.get("defaultThinkingLevel")).toBe(Effort.High);
	expect(settings.get("modelContextWindows")[main.models.delegate_model]).toBe(872000);
	settings.set("tools.approvalMode", "always-ask");
	await settings.flush();
	const saved = YAML.parse(await Bun.file(file).text()) as StoredDocument;
	expect(saved.models).toBeUndefined();
	expect(
		(YAML.parse(await Bun.file(temp.join("config.yaml")).text()) as Record<string, unknown>).profile_models,
	).toBeUndefined();
	main.models.delegate_model = "nous/xiaomi/mimo-v2.6-pro";
	await Bun.write(temp.join("config.yaml"), YAML.stringify(main));
	await settings.reloadFromDisk();
	expect(settings.getModelRole("task")).toBe(main.models.delegate_model);
	settings.setModelRole("task", "openai-codex/gpt-6.1-sol");
	await settings.flush();
	const overridden = YAML.parse(await Bun.file(temp.join("config.yaml")).text()) as {
		profile_models: Record<string, StoredDocument["models"]>;
	};
	expect(overridden.profile_models.research.delegate_model).toBe("openai-codex/gpt-6.1-sol");
	expect((YAML.parse(await Bun.file(temp.join("config.yaml")).text()) as StoredDocument).models.delegate_model).toBe(
		main.models.delegate_model,
	);
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

test.each(["alpha", "beta"])("profile %s rebases legacy main-bank pins on load and save", async name => {
	process.env.MERCURY_HOME = temp.path();
	const mainBank = temp.join("memories/mnemopi.db");
	const home = temp.join(`hermes/profiles/${name}`);
	const file = path.join(home, "config.yaml");
	process.env.MERCURY_CONFIG = file;
	await Bun.write(file, YAML.stringify({ hermes: {}, omp: { mnemopi: { dbPath: mainBank } } }));
	const settings = await Settings.init({ agentDir: path.join(home, "omp"), cwd: temp.path() });
	const local = path.join(home, "memories/mnemopi.db");
	expect(settings.get("mnemopi.dbPath")).toBe(local);
	settings.set("tools.approvalMode", "write");
	await settings.flush();
	const saved = YAML.parse(await Bun.file(file).text()) as { omp: { mnemopi: { dbPath: string } } };
	expect(saved.omp.mnemopi.dbPath).toBe(local);
});

test("a complete central override controls native retries, effort and context without main-slot leakage", async () => {
	process.env.MERCURY_HOME = temp.path();
	const file = temp.join("hermes/profiles/research/config.yaml");
	process.env.MERCURY_CONFIG = file;
	const main = {
		models: {
			default: "openrouter/main-chat",
			delegate_model: "openrouter/main-code",
			delegate_fallback: "openrouter/main-retry",
		},
		profile_models: {
			research: {
				default: "nous/vendor/chat",
				fallback: "",
				delegate_model: "nous/vendor/code",
				delegate_fallback: "",
				reasoning_overrides: { "nous/vendor/code": "medium" },
				context_windows: { "nous/vendor/code": 200000 },
			},
		},
	};
	await Bun.write(temp.join("config.yaml"), YAML.stringify(main));
	await Bun.write(
		file,
		YAML.stringify({
			models: { delegate_model: "openrouter/stale" },
			hermes: {},
			omp: { delegateModel: "openrouter/native-stale", tools: { approvalMode: "write" } },
		}),
	);
	const settings = await Settings.init({ agentDir: temp.join("hermes/profiles/research/omp"), cwd: temp.path() });
	expect(settings.getModelRole("task")).toBe("nous/vendor/code");
	expect(settings.get("retry.fallbackChains")).toEqual({ "nous/vendor/code": [] });
	expect(settings.get("defaultThinkingLevel")).toBe(Effort.Medium);
	expect(settings.get("modelContextWindows")["nous/vendor/code"]).toBe(200000);
	settings.set("tools.approvalMode", "yolo");
	await settings.flush();
	expect(YAML.parse(await Bun.file(temp.join("config.yaml")).text())).toEqual(main);
	settings.setModelRole("task", "openai-codex/new-code");
	await settings.flush();
	const saved = YAML.parse(await Bun.file(temp.join("config.yaml")).text()) as typeof main;
	expect(saved.models).toEqual(main.models);
	expect(saved.profile_models.research.default).toBe("nous/vendor/chat");
	expect(saved.profile_models.research.delegate_model).toBe("openai-codex/new-code");
	expect((YAML.parse(await Bun.file(file).text()) as Record<string, unknown>).models).toBeUndefined();
});

test.each([null, {}, { default: "nous/chat" }, { default: "broken", delegate_model: "nous/code" }])(
	"an invalid explicit profile configuration fails instead of selecting a main model: %j",
	async invalid => {
		process.env.MERCURY_HOME = temp.path();
		const file = temp.join("hermes/profiles/research/config.yaml");
		process.env.MERCURY_CONFIG = file;
		await Bun.write(
			temp.join("config.yaml"),
			YAML.stringify({ models: { delegate_model: "openrouter/main" }, profile_models: { research: invalid } }),
		);
		await Bun.write(file, YAML.stringify({ hermes: {}, omp: {} }));
		await expect(
			Settings.init({ agentDir: temp.join("hermes/profiles/research/omp"), cwd: temp.path() }),
		).rejects.toThrow("profile_models.research");
	},
);
