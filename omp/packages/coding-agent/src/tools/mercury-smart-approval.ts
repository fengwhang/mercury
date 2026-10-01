/** Share Hermes's command risk assessment without handing it the terminal. */
import * as path from "node:path";

export interface MercuryCommandAssessment {
	policy: "allow" | "deny" | "prompt";
	reason?: string;
}

export async function assessMercuryCommand(
	command: string,
	cwd: string,
	configPath: string,
	session: string,
	signal?: AbortSignal,
): Promise<MercuryCommandAssessment> {
	signal?.throwIfAborted();
	try {
		return await exchange(command, cwd, configPath, session, signal);
	} catch {
		signal?.throwIfAborted();
		return { policy: "prompt", reason: "Smart assessment unavailable; manual approval required" };
	}
}

async function exchange(
	command: string,
	cwd: string,
	configPath: string,
	session: string,
	signal?: AbortSignal,
): Promise<MercuryCommandAssessment> {
	const repo = process.env.MERCURY_REPO?.trim();
	if (!repo) return { policy: "prompt", reason: "Mercury runtime unavailable; manual approval required" };
	signal?.throwIfAborted();
	const child = Bun.spawn([process.env.MERCURY_PYTHON?.trim() || "python3", "-m", "mercury_cli.omp_approval"], {
		cwd,
		env: {
			...process.env,
			MERCURY_CONFIG: configPath,
			PYTHONPATH: [path.join(repo, "hermes"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
		},
		stdin: "pipe",
		stdout: "pipe",
		stderr: "ignore",
	});
	// Cancellation stops only this pre-execution assessment subprocess.
	const abort = () => child.kill();
	signal?.addEventListener("abort", abort, { once: true });
	if (signal?.aborted) abort();
	try {
		child.stdin.write(JSON.stringify({ command, session }));
		child.stdin.end();
		const [output, exit] = await Promise.all([new Response(child.stdout).text(), child.exited]);
		signal?.throwIfAborted();
		if (exit !== 0) throw new Error("Assessment failed");
		const reply: unknown = JSON.parse(output);
		if (!reply || typeof reply !== "object") throw new Error("Invalid assessment");
		const result = reply as Record<string, unknown>;
		if (result.policy !== "allow" && result.policy !== "deny" && result.policy !== "prompt") {
			throw new Error("Invalid assessment policy");
		}
		return { policy: result.policy, ...(typeof result.reason === "string" ? { reason: result.reason } : {}) };
	} finally {
		signal?.removeEventListener("abort", abort);
		child.kill();
	}
}
