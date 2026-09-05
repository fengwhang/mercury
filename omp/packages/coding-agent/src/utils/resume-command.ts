import { $env, APP_NAME, getActiveProfile } from "@oh-my-pi/pi-utils";

/**
 * Build the shell command that resumes a session by id.
 *
 * Sessions launched under a named profile are stored in that profile's agent
 * directory (`~/.omp/profiles/<name>/agent`), so a bare `omp --resume <id>`
 * run without the profile looks in the default directory and fails with
 * `Session "<id>" not found`. When a profile is active, prefix `--profile
 * <name>` so the emitted hint is a command the user can paste verbatim
 * (issue #9018). Profile names are validated against a strict charset
 * (`normalizeProfileName`), so no shell quoting is required.
 *
 * HERMES-OMP PATCH (Mercury embedding): when running inside Mercury
 * (MERCURY_HOME set — always exported by the `mercury omp` passthrough and
 * the delegate spawner), the user-facing command is `mercury omp …`, not a
 * bare `omp …` the user cannot run: omp is embedded, never a peer product.
 */
export function resumeCommand(sessionId: string): string {
	const mercury = !!($env.MERCURY_HOME ?? "").trim();
	const profile = getActiveProfile();
	const profileFlag = profile ? `--profile ${profile} ` : "";
	if (mercury) return `mercury omp ${profileFlag}--resume ${sessionId}`;
	return `${APP_NAME} ${profileFlag}--resume ${sessionId}`;
}
