import { describe, expect, it } from "bun:test";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import type { ModelRegistry } from "../src/config/model-registry";
import { Settings } from "../src/config/settings";
import { resolveCliArgv } from "../src/cli-commands";
import { configuredJudgmentCandidates, resolveJudge } from "../src/judgment";

const primary = getBundledModel("openai", "gpt-4o-mini")!;
const fallback = getBundledModel("google", "gemini-2.5-flash")!;
const unrelated = getBundledModel("openrouter", "google/gemini-2.5-flash")!;
const selector = `${primary.provider}/${primary.id}`;
const fallbackSelector = `${fallback.provider}/${fallback.id}`;
const registry = {
	getAvailable: () => [unrelated, fallback, primary],
	find: (provider: string, id: string) =>
		[primary, fallback, unrelated].find(m => m.provider === provider && m.id === id),
	hasProvider: (provider: string) => [primary, fallback, unrelated].some(m => m.provider === provider),
} as unknown as ModelRegistry;

describe("semantic search task routing", () => {
	it("prices and routes the configured task model before only its declared fallbacks", () => {
		const settings = Settings.isolated({
			delegateModel: selector,
			"retry.modelFallback": true,
			"retry.fallbackChains": { [selector]: [fallbackSelector] },
		});
		const judge = resolveJudge({ settings, registry, purpose: "find" });
		expect(judge.primaryModel()).toBe(primary);
		expect(configuredJudgmentCandidates(settings, registry).map(c => c.model)).toEqual([primary, fallback]);
		expect(configuredJudgmentCandidates(settings, registry, fallback).map(c => c.model)).not.toContain(unrelated);
	});
	it("does not switch to another credentialed model when configuration is missing or invalid", () => {
		expect(() => configuredJudgmentCandidates(Settings.isolated({ delegateModel: "" }), registry)).toThrow(
			"configure Mercury's delegate model",
		);
		expect(() =>
			configuredJudgmentCandidates(Settings.isolated({ delegateModel: "unknown/missing" }), registry),
		).toThrow("configured task model");
	});
	it("honors disabled model fallback even when another configured model is available", () => {
		const settings = Settings.isolated({
			delegateModel: selector,
			delegateFallback: fallbackSelector,
			"retry.modelFallback": false,
		});
		expect(configuredJudgmentCandidates(settings, registry).map(c => c.model)).toEqual([primary]);
	});
});

it("drops --no-prewalk when hoisting a command with its own flag surface", () => {
	expect(resolveCliArgv(["--no-prewalk", "toks", "hello"])).toEqual({ argv: ["toks", "hello"] });
});
