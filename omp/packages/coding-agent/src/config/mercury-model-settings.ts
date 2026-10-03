/** Transient native settings; the Mercury models block owns persisted choices. */
import * as fs from "node:fs";
import * as path from "node:path";
import { YAML } from "bun";
import { parseModelString } from "./model-resolver";
import type { RawSettings } from "./settings";

function record(value: unknown): Record<string, unknown> {
	return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function inheritsModels(whole: RawSettings, configPath?: string): boolean {
	const root = process.env.MERCURY_HOME?.trim();
	return (
		!!root &&
		!!configPath &&
		path.dirname(path.dirname(path.resolve(configPath))) === path.resolve(root, "hermes", "profiles") &&
		record(whole.profile).inherit_models !== false
	);
}

function effectiveModels(whole: RawSettings, configPath?: string): Record<string, unknown> {
	const local = record(whole.models);
	if (!inheritsModels(whole, configPath)) return local;
	const mainPath = path.resolve(process.env.MERCURY_HOME!, "config.yaml");
	let main: Record<string, unknown>;
	try {
		main = record(YAML.parse(fs.readFileSync(mainPath, "utf8")));
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return local;
		throw error;
	}
	const base = record(main.models);
	return {
		...base,
		...local,
		reasoning_overrides: { ...record(base.reasoning_overrides), ...record(local.reasoning_overrides) },
		context_windows: { ...record(base.context_windows), ...record(local.context_windows) },
	};
}

export function projectMercuryModels(whole: RawSettings, configPath?: string): RawSettings {
	const native = structuredClone(record(whole.omp));
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
	const models = { ...record(result.models) };
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
	result.models = models;
	result.omp = settings;
	return result;
}
