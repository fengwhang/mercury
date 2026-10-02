import type { Api, Model } from "@oh-my-pi/pi-catalog/types";
import { applyModelOverride } from "./model-patch";

/** Apply an explicit Mercury model budget, or the provider maximum on opt-in. */
export function applyContextWindow(model: Model<Api>, windows: Record<string, number>, extended: boolean): Model<Api> {
	const selected = windows[`${model.provider}/${model.id}`];
	const window = selected ?? (extended ? model.maxContextWindow : undefined);
	if (typeof window !== "number" || !Number.isSafeInteger(window) || window <= 0) return model;
	return applyModelOverride(model, {
		contextWindow: model.maxContextWindow ? Math.min(window, model.maxContextWindow) : window,
	});
}
