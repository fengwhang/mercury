/** Transient native settings; the Mercury models block owns persisted choices. */
import * as fs from "node:fs";
import * as path from "node:path";
import { randomUUID } from "node:crypto";
import { withFileLock } from "@oh-my-pi/pi-utils/file-lock";
import { replaceFileAtomically } from "../utils/atomic-file";
import { YAML } from "bun";
import { mercuryProfileBankPath } from "./mercury-memory-settings";
import { parseModelString } from "./model-resolver";
import type { RawSettings } from "./settings";

function record(value: unknown): Record<string, unknown> {
	return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function profileModelLocation(configPath?: string): { mainPath: string; name: string } | undefined {
	const root = process.env.MERCURY_HOME?.trim();
	if (!root || !configPath) return undefined;
	const home = path.dirname(path.resolve(configPath));
	if (path.dirname(home) !== path.resolve(root, "hermes", "profiles")) return undefined;
	return { mainPath: path.resolve(root, "config.yaml"), name: path.basename(home) };
}

function inheritsModels(_whole: RawSettings, configPath?: string): boolean {
	return profileModelLocation(configPath) !== undefined;
}

function validateProfileModels(value: unknown, location: string): Record<string, unknown> {
	const fail = (message: string): never => {
		throw new Error(
			`${location}: ${message}; fix this profile's models or remove its profile_models entry to inherit`,
		);
	};
	if (!value || typeof value !== "object" || Array.isArray(value)) fail("expected a model mapping");
	const models = structuredClone(record(value));
	for (const key of ["default", "fallback", "delegate_model", "delegate_fallback"]) {
		const selector = models[key] ?? "";
		if (typeof selector !== "string") fail(`${key} must be a provider/model string`);
		if ((key === "default" || key === "delegate_model") && !selector) fail(`${key} is required`);
		if (selector && (!/^[^/\s]+\/\S+$/.test(String(selector)) || String(selector).startsWith("auto/")))
			fail(`${key} must include provider/model identity`);
		models[key] = selector;
	}
	for (const [primary, fallback, chainKey] of [
		["default", "fallback", "fallback_chain"],
		["delegate_model", "delegate_fallback", "delegate_fallback_chain"],
	]) {
		const chain = models[chainKey] ?? [];
		if (!Array.isArray(chain) || chain.some(item => typeof item !== "string" || !/^[^/\s]+\/\S+$/.test(item)))
			fail(`${chainKey} must contain provider/model identities`);
		const entries = chain as string[];
		if (models[fallback] && models[fallback] === models[primary]) fail(`${fallback} must differ from ${primary}`);
		if (
			entries.length &&
			(new Set(entries).size !== entries.length ||
				entries.includes(String(models[primary])) ||
				entries[0] !== models[fallback])
		)
			fail(`${chainKey} must start with ${fallback}, without duplicates or ${primary}`);
		models[chainKey] = entries;
	}
	const efforts = models.reasoning_overrides ?? {};
	if (
		!efforts ||
		typeof efforts !== "object" ||
		Array.isArray(efforts) ||
		Object.values(efforts).some(v => typeof v !== "string" || !v)
	)
		fail("reasoning_overrides must map model identities to effort strings");
	models.reasoning_overrides = efforts;
	const windows = models.context_windows ?? {};
	if (
		!windows ||
		typeof windows !== "object" ||
		Array.isArray(windows) ||
		Object.values(windows).some(v => typeof v !== "number" || !Number.isSafeInteger(v) || v <= 0)
	)
		fail("context_windows must map model identities to positive token limits");
	models.context_windows = windows;
	return models;
}

function selectedProfileModels(main: Record<string, unknown>, name: string, mainPath: string): Record<string, unknown> {
	if (main.profile_models !== undefined) {
		if (!main.profile_models || typeof main.profile_models !== "object" || Array.isArray(main.profile_models))
			throw new Error(`${mainPath}: profile_models must be a mapping`);
		const entries = record(main.profile_models);
		if (Object.hasOwn(entries, name))
			return validateProfileModels(entries[name], `${mainPath}: profile_models.${name}`);
	}
	return record(main.models);
}

function effectiveModels(whole: RawSettings, configPath?: string): Record<string, unknown> {
	const location = profileModelLocation(configPath);
	if (!location) return record(whole.models);
	let main: Record<string, unknown>;
	try {
		const parsed: unknown = YAML.parse(fs.readFileSync(location.mainPath, "utf8"));
		if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
			throw new Error("Expected a configuration mapping");
		main = record(parsed);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") main = {};
		else
			throw new Error(
				`Cannot resolve profile '${location.name}' models from ${location.mainPath}: ${String(error)}`,
			);
	}
	return {
		default: "",
		fallback: "",
		delegate_model: "",
		delegate_fallback: "",
		fallback_chain: [],
		delegate_fallback_chain: [],
		reasoning_overrides: {},
		context_windows: {},
		...selectedProfileModels(main, location.name, location.mainPath),
	};
}

export function projectMercuryModels(whole: RawSettings, configPath?: string): RawSettings {
	const native = structuredClone(record(whole.omp));
	const memory = record(native.mnemopi);
	const bank = mercuryProfileBankPath(
		typeof memory.dbPath === "string" ? memory.dbPath : undefined,
		configPath ? path.dirname(configPath) : undefined,
	);
	if (bank) native.mnemopi = { ...memory, dbPath: bank };
	const models = effectiveModels(whole, configPath);
	const selector = models.delegate_model;
	const efforts = record(models.reasoning_overrides);
	if (models.reasoning_overrides !== undefined) native.modelReasoningOverrides = efforts;
	if (typeof selector === "string") {
		native.delegateModel = selector;
		delete native.modelRoles;
		const level = efforts[selector];
		if (typeof level === "string" && level !== "off") native.defaultThinkingLevel = level;
		else if (models.reasoning_overrides !== undefined) delete native.defaultThinkingLevel;
		const configured = models.delegate_fallback_chain;
		const chain = Array.isArray(configured) && configured.length ? configured : [models.delegate_fallback];
		const fallbacks = chain
			.filter((entry): entry is string => typeof entry === "string" && !!entry)
			.map(entry => (typeof efforts[entry] === "string" ? `${entry}:${efforts[entry]}` : entry));
		native.delegateFallback = fallbacks[0] ?? "";
		native.retry = { ...record(native.retry), fallbackChains: selector ? { [selector]: fallbacks } : {} };
	}
	if (models.context_windows && typeof models.context_windows === "object") {
		native.modelContextWindows = models.context_windows;
	}
	return native;
}

export function persistMercuryModels(whole: RawSettings, native: RawSettings, configPath?: string): RawSettings {
	const result = structuredClone(whole);
	const before = projectMercuryModels(whole, configPath);
	const inheriting = inheritsModels(whole, configPath);
	const initial = effectiveModels(whole, configPath);
	const models = inheriting ? structuredClone(initial) : { ...record(result.models) };
	const task = native.delegateModel;
	if (!inheriting && models.delegate_model === undefined && typeof task === "string") models.delegate_model = task;
	if (task !== before.delegateModel) {
		if (typeof task === "string") {
			const parsed = parseModelString(task, { allowMaxSuffix: true, allowAutoAlias: true });
			models.delegate_model = parsed ? `${parsed.provider}/${parsed.id}` : task;
			if (parsed?.thinkingLevel) {
				models.reasoning_overrides = {
					...record(models.reasoning_overrides),
					[models.delegate_model as string]: parsed.thinkingLevel,
				};
			}
		} else models.delegate_model = "";
	}
	if (
		(native.defaultThinkingLevel !== before.defaultThinkingLevel ||
			(!inheriting && record(models.reasoning_overrides)[task as string] === undefined)) &&
		typeof native.defaultThinkingLevel === "string" &&
		typeof task === "string"
	) {
		models.reasoning_overrides = {
			...record(models.reasoning_overrides),
			[task]: native.defaultThinkingLevel,
		};
	}
	if (
		native.modelContextWindows &&
		((!inheriting && models.context_windows === undefined) ||
			!Bun.deepEquals(native.modelContextWindows, before.modelContextWindows))
	) {
		if (!inheriting) models.context_windows = native.modelContextWindows;
		else {
			const windows = { ...record(models.context_windows) };
			const previous = record(before.modelContextWindows);
			const incoming = record(native.modelContextWindows);
			for (const key of new Set([...Object.keys(previous), ...Object.keys(incoming)])) {
				if (incoming[key] === previous[key]) continue;
				if (typeof incoming[key] === "number") windows[key] = incoming[key];
				else delete windows[key];
			}
			models.context_windows = windows;
		}
	}
	const oldEfforts = record(before.modelReasoningOverrides);
	const newEfforts = record(native.modelReasoningOverrides);
	if (!inheriting && models.reasoning_overrides === undefined && Object.keys(newEfforts).length)
		models.reasoning_overrides = { ...newEfforts };
	if (!Bun.deepEquals(oldEfforts, newEfforts)) {
		const efforts = { ...record(models.reasoning_overrides) };
		for (const key of new Set([...Object.keys(oldEfforts), ...Object.keys(newEfforts)])) {
			if (newEfforts[key] === oldEfforts[key]) continue;
			if (typeof newEfforts[key] === "string") efforts[key] = newEfforts[key];
			else delete efforts[key];
		}
		models.reasoning_overrides = efforts;
	}
	const oldChains = record(record(before.retry).fallbackChains);
	const newChains = record(record(native.retry).fallbackChains);
	const selected = typeof task === "string" ? task : "";
	let fallback: unknown[] | undefined;
	if (native.delegateFallback !== before.delegateFallback) {
		fallback =
			typeof native.delegateFallback === "string" && native.delegateFallback ? [native.delegateFallback] : [];
	} else if (!Bun.deepEquals(newChains, oldChains)) {
		const chain = newChains[selected];
		fallback = Array.isArray(chain) ? chain : [];
	}
	if (fallback) {
		const selectors = fallback
			.filter((entry): entry is string => typeof entry === "string" && !!entry)
			.map(entry => {
				const parsed = parseModelString(entry, { allowMaxSuffix: true, allowAutoAlias: true });
				const selector = parsed ? `${parsed.provider}/${parsed.id}` : entry;
				if (parsed?.thinkingLevel)
					models.reasoning_overrides = { ...record(models.reasoning_overrides), [selector]: parsed.thinkingLevel };
				return selector;
			});
		models.delegate_fallback = selectors[0] ?? "";
		models.delegate_fallback_chain = selectors.length > 1 ? selectors : [];
	}
	const settings = structuredClone(native);
	delete settings.modelRoles;
	delete settings.delegateModel;
	delete settings.delegateFallback;
	delete settings.defaultThinkingLevel;
	delete settings.modelReasoningOverrides;
	delete settings.modelContextWindows;
	if (settings.retry) delete record(settings.retry).fallbackChains;
	if (!inheriting || !Bun.deepEquals(models, initial)) result.models = models;
	else delete result.models;
	result.omp = settings;
	return result;
}

/** Persist native model edits in the installation authority, keeping behavior local. */
export async function persistMercuryModelDocument(
	whole: RawSettings,
	native: RawSettings,
	configPath: string,
): Promise<RawSettings> {
	const result = persistMercuryModels(whole, native, configPath);
	const location = profileModelLocation(configPath);
	if (!location || !result.models) return result;
	const before = effectiveModels(whole, configPath);
	const incoming = record(result.models);
	await withFileLock(location.mainPath, async () => {
		const parsed: unknown = YAML.parse(await Bun.file(location.mainPath).text());
		if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
			throw new Error(`Expected a configuration mapping: ${location.mainPath}`);
		const main = record(parsed);
		const models: Record<string, unknown> = structuredClone({
			default: "",
			fallback: "",
			delegate_model: "",
			delegate_fallback: "",
			fallback_chain: [],
			delegate_fallback_chain: [],
			reasoning_overrides: {},
			context_windows: {},
			...selectedProfileModels(main, location.name, location.mainPath),
		});
		for (const [key, value] of Object.entries(incoming)) {
			if (Bun.deepEquals(value, before[key])) continue;
			if (key === "reasoning_overrides" || key === "context_windows") {
				const merged = { ...record(models[key]) };
				const old = record(before[key]);
				const next = record(value);
				for (const selector of new Set([...Object.keys(old), ...Object.keys(next)])) {
					if (Bun.deepEquals(next[selector], old[selector])) continue;
					if (Object.hasOwn(next, selector)) merged[selector] = next[selector];
					else delete merged[selector];
				}
				models[key] = merged;
			} else models[key] = value;
		}
		const entries = record(main.profile_models);
		const validated = validateProfileModels(models, `${location.mainPath}: profile_models.${location.name}`);
		entries[location.name] = Object.fromEntries(
			Object.entries(validated).filter(
				([key, value]) =>
					["default", "fallback", "delegate_model", "delegate_fallback"].includes(key) ||
					(Array.isArray(value)
						? value.length > 0
						: typeof value === "object" && value !== null
							? Object.keys(value).length > 0
							: !!value),
			),
		);
		main.profile_models = entries;
		const temporary = `${location.mainPath}.${process.pid}.${randomUUID()}.tmp`;
		try {
			await fs.promises.writeFile(temporary, YAML.stringify(main), { mode: 0o600, flag: "wx" });
			await replaceFileAtomically(temporary, location.mainPath);
		} finally {
			await fs.promises.rm(temporary, { force: true });
		}
	});
	delete result.models;
	return result;
}
