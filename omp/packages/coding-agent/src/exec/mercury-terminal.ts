/** Run Mercury shell commands through the same configured backends as Hermes. */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { createInterface } from "node:readline";
import { YAML } from "bun";
import { logger, postmortem } from "@oh-my-pi/pi-utils";
import type { BashExecutorOptions, BashResult } from "./bash-executor";
import { OutputSink } from "../session/streaming-output";

function mapping(value: unknown, label: string): Record<string, unknown> {
	if (value === undefined || value === null) return {};
	if (typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be a mapping`);
	return value as Record<string, unknown>;
}

export function mercuryTerminalBackend(env: NodeJS.ProcessEnv = process.env): string {
	if (!env.MERCURY_HOME && !env.MERCURY_CONFIG) return "local";
	const configPath = env.MERCURY_CONFIG || path.join(env.MERCURY_HOME!, "config.yaml");
	let whole: Record<string, unknown> = {};
	try {
		whole = mapping(YAML.parse(fs.readFileSync(configPath, "utf8")), "Mercury config");
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
	const hermes = mapping(whole.hermes, "hermes");
	const terminal = mapping(hermes.terminal ?? whole.terminal, "terminal");
	const selected = terminal.backend ?? env.TERMINAL_ENV ?? "local";
	if (typeof selected !== "string" || !selected.trim()) throw new Error("terminal.backend must be a backend name");
	return selected.trim().toLowerCase();
}

interface TerminalReply {
	id: number;
	chunk?: string;
	output?: string;
	returncode?: number;
	cwd?: string;
	error?: string;
}

class TerminalWorker {
	readonly process: ChildProcessWithoutNullStreams;
	#nextId = 0;
	#pending = new Map<
		number,
		{ resolve: (value: TerminalReply) => void; reject: (error: Error) => void; onChunk?: (chunk: string) => void }
	>();
	#dead = false;

	#reference(active: boolean): void {
		if (active) this.process.ref();
		else this.process.unref();
		for (const stream of [this.process.stdin, this.process.stdout, this.process.stderr]) {
			const handle = stream as typeof stream & { ref?: () => void; unref?: () => void };
			if (active) handle.ref?.();
			else handle.unref?.();
		}
	}

	constructor(backend: string) {
		const repo = process.env.MERCURY_REPO || path.join(process.env.MERCURY_HOME!, "mercury-agent");
		const python = process.env.MERCURY_PYTHON || path.join(repo, "hermes", ".venv", "bin", "python");
		const worker = path.join(repo, "hermes", "tools", "omp_terminal_worker.py");
		if (!fs.existsSync(worker)) throw new Error("Mercury terminal bridge missing; reinstall Mercury");
		this.process = spawn(python, ["-u", worker, backend], { stdio: "pipe", env: process.env });
		createInterface({ input: this.process.stdout }).on("line", line => {
			try {
				const reply = JSON.parse(line) as TerminalReply;
				const pending = this.#pending.get(reply.id);
				if (!pending) return;
				if (reply.chunk !== undefined) {
					pending.onChunk?.(reply.chunk);
					return;
				}
				this.#pending.delete(reply.id);
				if (reply.error) pending.reject(new Error(reply.error));
				else pending.resolve(reply);
				if (!this.#pending.size) this.#reference(false);
			} catch (error) {
				this.#fail(new Error(`Invalid Mercury terminal response: ${String(error)}`));
			}
		});
		// Backend/library diagnostics belong in the log, never RPC stdout.
		this.process.stderr.on("data", chunk => logger.debug("Mercury terminal backend", { detail: String(chunk) }));
		this.process.on("error", error => this.#fail(error));
		this.process.on("exit", () => this.#fail(new Error("Mercury terminal backend disconnected; retry the command")));
		this.process.stdin.on("error", error => this.#fail(error));
		this.#reference(false);
	}

	#fail(error: Error): void {
		this.#dead = true;
		for (const pending of this.#pending.values()) pending.reject(error);
		this.#pending.clear();
	}

	get dead(): boolean {
		return this.#dead;
	}

	async run(
		command: string,
		options?: BashExecutorOptions,
		onChunk?: (chunk: string) => void,
	): Promise<TerminalReply> {
		if (this.#dead) throw new Error("Mercury terminal worker is unavailable");
		const id = ++this.#nextId;
		this.#reference(true);
		const { promise: result, resolve, reject } = Promise.withResolvers<TerminalReply>();
		this.#pending.set(id, { resolve, reject, onChunk });
		const abort = () => this.process.stdin.write(`${JSON.stringify({ cancel: id })}\n`);
		options?.signal?.addEventListener("abort", abort, { once: true });
		this.process.stdin.write(
			`${JSON.stringify({
				id,
				command,
				cwd: options?.cwd,
				env: options?.env,
				timeout: options?.timeout ?? 300_000,
			})}\n`,
		);
		if (options?.signal?.aborted) abort();
		try {
			return await result;
		} finally {
			options?.signal?.removeEventListener("abort", abort);
		}
	}

	close(): void {
		this.process.stdin.end();
	}
}

const workers = new Map<string, TerminalWorker>();
postmortem.register("mercury-terminal", () => {
	for (const worker of workers.values()) worker.close();
	workers.clear();
});

/** Shared by tests and normal teardown; steering never calls this. */
export function closeMercuryTerminals(sessionKey?: string): void {
	for (const [key, worker] of workers) {
		const owner = (JSON.parse(key) as (string | null)[])[3];
		if (sessionKey !== undefined && owner !== sessionKey && !owner?.startsWith(`${sessionKey}:async:`)) continue;
		worker.close();
		workers.delete(key);
	}
}

export async function executeMercuryTerminal(command: string, options?: BashExecutorOptions): Promise<BashResult> {
	const backend = mercuryTerminalBackend();
	if (backend === "local") throw new Error("Remote terminal executor requires a non-local backend");
	const key = JSON.stringify([process.env.MERCURY_CONFIG, process.env.MERCURY_HOME, backend, options?.sessionKey]);
	let worker = workers.get(key);
	if (!worker || worker.dead) {
		worker = new TerminalWorker(backend);
		workers.set(key, worker);
	}
	const sink = new OutputSink({
		onChunk: options?.onChunk,
		artifactPath: options?.artifactPath,
		artifactId: options?.artifactId,
	});
	try {
		const reply = await worker.run(command, options, chunk => sink.push(chunk));
		sink.replace(reply.output ?? "");
		return {
			...(await sink.dump()),
			exitCode: reply.returncode,
			cancelled: reply.returncode === 130,
			timedOut: reply.returncode === 124,
			workingDir: reply.cwd,
		};
	} finally {
		await sink.dispose();
	}
}
