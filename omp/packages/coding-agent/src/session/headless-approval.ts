// HERMES-OMP PATCH (approval pass-through, user directive): headless approval
// bridge. When a hermes delegate spawns omp in ONE-SHOT/print mode, print mode
// has no UI context — approval gates previously resolved as "denied by user"
// and the child died silently. This module routes approval selects/confirms
// to the hermes parent (MERCURY_APPROVAL_SOCKET), which runs its guard stack
// and surfaces the prompt to the USER through the normal chat channel.
//
// Wire shape (HTTP over the Unix socket, matching the repo's blob-broker
// pattern):
//   POST /approve  {"kind": "select", "title": <prompt with Command: line>}
//   -> 200 {"value": "Approve"|"Deny"}
//   POST /approve  {"kind": "confirm", "title": ..., "message": ...}
//   -> 200 {"confirmed": true|false}
const APPROVAL_TIMEOUT_MS = 600_000; // 10 min: a human may be slow.

export function mercuryApprovalSocket(): string | undefined {
	const v = process.env.MERCURY_APPROVAL_SOCKET?.trim();
	return v || undefined;
}

/** True when the headless approval bridge should be used (child side). */
export function headlessApprovalEnabled(): boolean {
	return mercuryApprovalSocket() !== undefined;
}

interface ApprovalReply {
	value?: string;
	confirmed?: boolean;
}

async function askParent(payload: Record<string, unknown>): Promise<ApprovalReply> {
	const socketPath = mercuryApprovalSocket();
	if (!socketPath) return {};
	try {
		const response = await fetch(`http://mercury-approval.local/approve`, {
			method: "POST",
			unix: socketPath,
			headers: { "content-type": "application/json" },
			body: JSON.stringify(payload),
			signal: AbortSignal.timeout(APPROVAL_TIMEOUT_MS),
		});
		if (!response.ok) return {};
		return (await response.json()) as ApprovalReply;
	} catch {
		return {}; // bridge failure -> fail closed (deny)
	}
}

/** Select via the hermes parent; undefined denies (wrapper throws deny). */
export async function headlessSelect(title: string, options: string[]): Promise<string | undefined> {
	if (options.length === 2 && options[0] === "Approve" && options[1] === "Deny") {
		const reply = await askParent({ kind: "select", title });
		if (reply.value === "Approve" || reply.value === "Deny") return reply.value;
	}
	// Non-approval selects have no human path headless: cancel.
	return undefined;
}

/** Confirm via the hermes parent; defaults false. */
export async function headlessConfirm(title: string, message: string): Promise<boolean> {
	const reply = await askParent({ kind: "confirm", title, message });
	return reply.confirmed === true;
}

export const headlessApprovalUIContext = {
	select: headlessSelect,
	confirm: headlessConfirm,
	input: async () => undefined,
	notify: () => {},
	onTerminalInput: () => () => {},
	setStatus: () => {},
	setWorkingMessage: () => {},
	setWidget: () => {},
	setTitle: () => {},
	custom: async () => undefined as never,
	setEditorText: () => {},
	pasteToEditor: () => {},
};
