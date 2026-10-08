interface DelegationCheckpointSession {
	getLastAssistantMessage():
		| {
				stopReason: string;
				content: ReadonlyArray<{ type: string; text?: string }>;
				errorMessage?: string;
		  }
		| undefined;
	sessionManager: {
		appendCustomEntry(customType: string, data?: unknown): unknown;
		flushSync(): void;
	};
}

/** A final message can precede a queued continuation. Only terminal agent_end closes the task. */
export function recordRpcDelegationEvent(
	session: DelegationCheckpointSession,
	event: { type: string; isTerminal?: boolean },
	childId: string | undefined,
): void {
	if (!childId) return;
	if (event.type === "agent_start") {
		session.sessionManager.appendCustomEntry("mercury_delegation_started", { childId });
		session.sessionManager.flushSync();
		return;
	}
	if (event.type !== "agent_end" || event.isTerminal === false) return;
	const message = session.getLastAssistantMessage();
	const summary = message?.content
		.filter(block => block.type === "text")
		.map(block => block.text ?? "")
		.join("\n");
	const status =
		message?.stopReason === "stop" && summary
			? "completed"
			: message?.stopReason === "aborted"
				? "interrupted"
				: "failed";
	session.sessionManager.appendCustomEntry("mercury_delegation_terminal", {
		childId,
		status,
		summary: status === "completed" ? summary : null,
		error:
			status === "completed"
				? null
				: (message?.errorMessage ?? `Terminal assistant stop: ${message?.stopReason ?? "missing"}`),
	});
	// The parent can disappear immediately after the wire event. Persist first.
	session.sessionManager.flushSync();
}
