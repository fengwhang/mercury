/**
 * Generate commit messages from diffs using the session model.
 * Follows the same pattern as title-generator.ts.
 */
import type { ThinkingLevel } from "@oh-my-pi/pi-agent-core";
import type { Api, Model } from "@oh-my-pi/pi-ai";
import { completeSimple, retryTransientCompletion } from "@oh-my-pi/pi-ai";
import { logger, prompt } from "@oh-my-pi/pi-utils";

import type { ModelRegistry } from "../config/model-registry";
import { resolveRoleSelection, getModelMatchPreferences, resolveModelRoleValue } from "../config/model-resolver";
import type { Settings } from "../config/settings";
import commitSystemPrompt from "../prompts/system/commit-message-system.md" with { type: "text" };
import { concreteThinkingLevel, toReasoningEffort } from "../thinking";

const COMMIT_SYSTEM_PROMPT = prompt.render(commitSystemPrompt);
const MAX_DIFF_CHARS = 4000;
// Cover the "backend ignores `disableReasoning`" case unconditionally: the
// static `model.reasoning` catalog flag can't distinguish a thinking model
// declared `reasoning: false` (e.g. Qwen3 served locally via llama.cpp) from
// one that never emits thinking. `maxTokens` is a hard cap — non-thinking
// completions still return in a handful of tokens (issue #4355).
const COMMIT_MAX_TOKENS = 1024;

/** File patterns that should be excluded from commit message generation diffs. */
const NOISE_SUFFIXES = [".lock", ".lockb", "-lock.json", "-lock.yaml"];

/** Strip diff hunks for noisy files that drown out real changes. */
function filterDiffNoise(diff: string): string {
	const lines = diff.split("\n");
	const filtered: string[] = [];
	let skip = false;
	for (const line of lines) {
		if (line.startsWith("diff --git ")) {
			const bPath = line.split(" b/")[1];
			skip = bPath != null && NOISE_SUFFIXES.some(s => bPath.endsWith(s));
		}
		if (!skip) filtered.push(line);
	}
	return filtered.join("\n");
}

function getModelCandidates(
	registry: ModelRegistry,
	settings: Settings,
): Array<{ model: Model<Api>; thinkingLevel?: ThinkingLevel }> {
	// HERMES-OMP PATCH (no model roles): the commit-message generator runs
	// the session model — the configured default. No "smol" chain exists.
	const resolved = resolveRoleSelection(settings, registry.getAvailable());
	return resolved ? [{ model: resolved.model, thinkingLevel: concreteThinkingLevel(resolved.thinkingLevel) }] : [];
}

/**
 * Generate a commit message from a unified diff.
 * Returns null if generation fails (caller should fall back to generic message).
 */
export async function generateCommitMessage(
	diff: string,
	registry: ModelRegistry,
	settings: Settings,
	sessionId?: string,
): Promise<string | null> {
	const candidates = getModelCandidates(registry, settings);
	if (candidates.length === 0) {
		logger.debug("commit-msg-generator: no model available");
		return null;
	}

	const cleanDiff = filterDiffNoise(diff);
	const truncatedDiff =
		cleanDiff.length > MAX_DIFF_CHARS ? `${cleanDiff.slice(0, MAX_DIFF_CHARS)}\n… (truncated)` : cleanDiff;
	if (!truncatedDiff.trim()) {
		logger.debug("commit-msg-generator: diff is empty after noise filtering");
		return null;
	}
	const userMessage = `<diff>\n${truncatedDiff}\n</diff>`;

	for (const candidate of candidates) {
		const apiKey = await registry.getApiKey(candidate.model, sessionId);
		if (!apiKey) continue;

		try {
			const maxTokens = COMMIT_MAX_TOKENS;
			const response = await retryTransientCompletion(() =>
				completeSimple(
					candidate.model,
					{
						systemPrompt: [COMMIT_SYSTEM_PROMPT],
						messages: [{ role: "user", content: userMessage, timestamp: Date.now() }],
					},
					{
						apiKey: registry.resolver(candidate.model, sessionId),
						sessionId,
						maxTokens,
						reasoning: toReasoningEffort(candidate.thinkingLevel),
					},
				),
			);

			if (response.stopReason === "error") {
				logger.debug("commit-msg-generator: error", { model: candidate.model.id, error: response.errorMessage });
				continue;
			}

			let msg = "";
			for (const content of response.content) {
				if (content.type === "text") msg += content.text;
			}
			msg = msg.trim();
			if (!msg) continue;

			// Clean up: remove wrapping quotes, backticks, trailing period
			msg = msg.replace(/^[`"']|[`"']$/g, "").replace(/\.$/, "");

			logger.debug("commit-msg-generator: generated", { model: candidate.model.id, msg });
			return msg;
		} catch (err) {
			logger.debug("commit-msg-generator: error", {
				model: candidate.model.id,
				error: err instanceof Error ? err.message : String(err),
			});
		}
	}

	return null;
}
