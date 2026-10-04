/**
 * Log in to a model provider from the terminal.
 *
 * Mercury route: credentials are owned by the Mercury unified setup /
 * credential bridge (`mercury auth add`), never by a direct OAuth flow
 * inside the vendored engine. This command execs the bridge with inherited
 * stdio and propagates its exit code; when no Mercury CLI is reachable it
 * fails hard rather than writing credentials anywhere else.
 */

import * as path from "node:path";
import { Args, Command } from "@oh-my-pi/pi-utils/cli";
import { loginHelp as commandHelp } from "../cli/command-help";

function resolveMercuryCli(): string | undefined {
	const onPath = Bun.which("mercury");
	if (onPath) return onPath;
	const repo = (process.env.MERCURY_REPO ?? "").trim();
	if (repo) {
		const candidate = path.join(repo, "bin", "mercury");
		return candidate;
	}
	return undefined;
}

export default class Login extends Command {
	static description = commandHelp.description;
	static args = {
		provider: Args.string({
			description: "Provider id (e.g. anthropic, openai-codex); omit to pick interactively",
			required: false,
		}),
	};

	static examples = [
		"# Log in through the Mercury credential bridge\n  mercury omp login",
		"# Log in to a specific provider\n  mercury omp login anthropic",
	];

	async run(): Promise<void> {
		const { args } = await this.parse(Login);
		const mercury = resolveMercuryCli();
		if (!mercury) {
			process.stderr.write(
				"mercury omp login: no Mercury CLI found (PATH or $MERCURY_REPO/bin/mercury).\n" +
					"Log in with `mercury auth add <provider>` from a Mercury checkout instead.\n",
			);
			process.exitCode = 1;
			return;
		}
		const bridgeArgs = ["auth", "add"];
		if (args.provider) {
			bridgeArgs.push(args.provider);
		} else {
			process.stderr.write(
				"Pick a provider: `mercury omp login <provider>` (e.g. anthropic, openai-codex),\n" +
					"or run `mercury setup` for interactive onboarding.\n",
			);
			process.exitCode = 2;
			return;
		}
		const proc = Bun.spawn([mercury, ...bridgeArgs], {
			stdin: "inherit",
			stdout: "inherit",
			stderr: "inherit",
		});
		process.exitCode = await proc.exited;
	}
}
