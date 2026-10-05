import { afterAll, beforeAll, describe, expect, it } from "bun:test";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage, Usage } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import type { Model } from "@oh-my-pi/pi-catalog/types";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import {
	type RecoveryCompactionResult,
	TurnRecovery,
	type TurnRecoveryHost,
} from "@oh-my-pi/pi-coding-agent/session/turn-recovery";
import { TempDir } from "@oh-my-pi/pi-utils";

/**
 * A usage limit must consult the configured fallback chain.
 *
 * Regression this pins: when a delegate model hits `usage_limit_reached`, the
 * room shows `Provider retries failed` (`auto_retry_end{success:false}`) and the
 * subagent dies ~2s after spawn — the retry budget burns out on the SAME model
 * and `retry_fallback_applied` never fires, even though Mercury wrote a chain
 * covering the active model. A usage limit is charged to the plan, not to a key,
 * so retrying the same model can never recover; the chain must be reachable.
 *
 * Mercury's exact live shape: `retry.fallbackChains` keyed by the model
 * selector (`openai-codex/gpt-6.1-sol`) mapping to `delegate_fallback`.
 */

const USAGE: Usage = {
	input: 0,
	output: 0,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 0,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

const USAGE_LIMIT_MESSAGE = "The usage limit has been reached (code=usage_limit_reached)";

function makeErrorMessage(model: Model, errorMessage: string): AssistantMessage {
	return {
		role: "assistant",
		content: [],
		api: model.api,
		provider: model.provider,
		model: model.id,
		usage: { ...USAGE },
		stopReason: "error",
		errorMessage,
		timestamp: Date.now(),
	};
}

function createHost(
	model: Model,
	modelRegistry: ModelRegistry,
	options: {
		fallbackChains?: Record<string, string[]>;
		messages?: readonly AgentMessage[];
	} = {},
): TurnRecoveryHost {
	const settings = Settings.isolated({
		"retry.fallbackChains": options.fallbackChains ?? {},
	});
	return {
		agent: { state: { messages: options.messages ?? [] } } as never,
		sessionManager: { getLastModelChangeRole: () => undefined } as never,
		persistedAssistantEntryId: () => undefined,
		settings,
		modelRegistry,
		configWarnings: [],
		model: () => model,
		contextFitsModel: () => true,
		textOutputCommitted: () => true,
		thinkingLevel: () => undefined,
		configuredThinkingLevel: () => undefined,
		setThinkingLevel: () => {},
		thinkingLevelCeiling: () => undefined,
		isDisposed: () => false,
		isStreaming: () => false,
		isCompacting: () => false,
		abortInProgress: () => false,
		streamingEditAbortTriggered: () => false,
		promptGeneration: () => 0,
		sessionId: () => "usage-limit-fallback-test",
		emitSessionEvent: async () => {},
		scheduleAgentContinue: () => {},
		waitForSessionMessagePersistence: async () => {},
		appendSessionMessage: () => {},
		sessionMessageAlreadyPersisted: () => false,
		setModelWithProviderSessionReset: async () => {},
		resetCurrentResponsesProviderSession: () => {},
		maybeAutoRedeemCodexReset: async () => false,
		runAutoCompaction: async () =>
			({ deferredHandoff: false, continuationScheduled: false }) as RecoveryCompactionResult,
		withBashBranchTransition: <T>(operation: () => T): T => operation(),
	};
}

describe("usage limits must reach the fallback chain", () => {
	const primary = getBundledModel("anthropic", "claude-sonnet-4-5");
	if (!primary) throw new Error("Expected bundled model claude-sonnet-4-5");
	const fallback = getBundledModel("openai", "gpt-4o-mini");
	if (!fallback) throw new Error("Expected bundled fallback model");

	const primarySelector = `${primary.provider}/${primary.id}`;
	const fallbackSelector = `${fallback.provider}/${fallback.id}`;

	let tempDir: TempDir;
	let authStorage: AuthStorage;
	let modelRegistry: ModelRegistry;

	beforeAll(async () => {
		tempDir = TempDir.createSync("@pi-usage-limit-fallback-");
		authStorage = await AuthStorage.create(tempDir.join("testauth.db"));
		authStorage.setRuntimeApiKey("anthropic", "test-key");
		authStorage.setRuntimeApiKey("openai", "test-key");
		modelRegistry = new ModelRegistry(authStorage, tempDir.join("models.yml"));
	});

	afterAll(() => {
		authStorage.close();
		tempDir.removeSync();
	});

	it("a usage-limit error on a chain-covered model is fallback-eligible", () => {
		const recovery = new TurnRecovery(
			createHost(primary, modelRegistry, {
				fallbackChains: { [primarySelector]: [fallbackSelector] },
			}),
		);
		const message = makeErrorMessage(primary, USAGE_LIMIT_MESSAGE);
		expect(recovery.isHardErrorFallbackEligible(message)).toBe(true);
	});

	it("the chain key is consulted and yields the configured fallback", () => {
		const recovery = new TurnRecovery(
			createHost(primary, modelRegistry, {
				fallbackChains: { [primarySelector]: [fallbackSelector] },
			}),
		);
		const keys = recovery.retryFallbackChainKeys(primarySelector);
		expect(keys.length).toBeGreaterThan(0);
		const candidates = recovery.findRetryFallbackCandidates(keys[0], primarySelector);
		expect(candidates.map(c => c.raw)).toContain(fallbackSelector);
	});

	it("with no chain configured the error is NOT fallback-eligible", () => {
		const recovery = new TurnRecovery(createHost(primary, modelRegistry, {}));
		const message = makeErrorMessage(primary, USAGE_LIMIT_MESSAGE);
		expect(recovery.isHardErrorFallbackEligible(message)).toBe(false);
	});

	it("an unrelated hard error behaves the same way (chain is the only gate)", () => {
		const covered = new TurnRecovery(
			createHost(primary, modelRegistry, {
				fallbackChains: { [primarySelector]: [fallbackSelector] },
			}),
		);
		const uncovered = new TurnRecovery(createHost(primary, modelRegistry, {}));
		const message = makeErrorMessage(primary, "connection refused");
		expect(covered.isHardErrorFallbackEligible(message)).toBe(true);
		expect(uncovered.isHardErrorFallbackEligible(message)).toBe(false);
	});
});
