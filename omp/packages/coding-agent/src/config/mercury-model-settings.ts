/** Transient native settings; the Mercury models block owns persisted choices. */
import { parseModelString } from "./model-resolver";
import type { RawSettings } from "./settings";

function record(value: unknown): Record<string, unknown> {
	return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function projectMercuryModels(whole: RawSettings): RawSettings {
	const native = structuredClone(record(whole.omp));
	const models = record(whole.models);
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

export function persistMercuryModels(whole: RawSettings, native: RawSettings): RawSettings {
	const result = structuredClone(whole);
	const before = projectMercuryModels(whole);
	const models = { ...record(result.models) };
	const task = native.delegateModel;
	if (models.delegate_model === undefined && typeof task === "string") models.delegate_model = task;
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
			record(models.reasoning_overrides)[models.delegate_model as string] === undefined) &&
		typeof native.defaultThinkingLevel === "string" &&
		typeof models.delegate_model === "string"
	) {
		models.reasoning_overrides = {
			...record(models.reasoning_overrides),
			[models.delegate_model]: native.defaultThinkingLevel,
		};
	}
	if (
		native.modelContextWindows &&
		(models.context_windows === undefined || !Bun.deepEquals(native.modelContextWindows, before.modelContextWindows))
	) {
		models.context_windows = native.modelContextWindows;
	}
	const oldEfforts = record(before.modelReasoningOverrides);
	const newEfforts = record(native.modelReasoningOverrides);
	if (models.reasoning_overrides === undefined && Object.keys(newEfforts).length)
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
	const selected = typeof models.delegate_model === "string" ? models.delegate_model : "";
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
