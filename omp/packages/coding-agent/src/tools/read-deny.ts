import * as os from "node:os";
import * as path from "node:path";

/**
 * Secret-bearing project-local environment file basenames. Blocked because
 * .env files routinely contain API keys, database passwords, and other
 * credentials. Mirrors hermes `agent/file_safety.py`
 * (`_BLOCKED_PROJECT_ENV_BASENAMES`) — keep the two lists in sync.
 * `.env.example` is documentation, not a secret, and stays readable.
 */
export const BLOCKED_ENV_BASENAMES: Record<string, true> = {
	".env": true,
	".env.local": true,
	".env.development": true,
	".env.production": true,
	".env.test": true,
	".env.staging": true,
	".envrc": true,
};

/**
 * Credential / secret store filenames matched exactly under a Mercury home.
 * Mirrors hermes `agent/file_safety.py` (`credential_file_names`) — keep the
 * two lists in sync. Includes the Matrix Observatory plaintext secrets
 * (0600): owner-credentials.json (owner password + access_token), tuwunel
 * configs (registration token), and the sidecar appservice registration
 * (as/hs tokens).
 */
const CREDENTIAL_FILE_NAMES: readonly string[] = [
	"auth.json",
	"auth.lock",
	".anthropic_oauth.json",
	".env",
	"webhook_subscriptions.json",
	"auth/google_oauth.json",
	"cache/bws_cache.json",
	"observatory/owner-credentials.json",
	"observatory/tuwunel.toml",
	"observatory/tuwunel-bootstrap.toml",
	"observatory/appservices/merc-observatory.yaml",
];

/** Directory names under a Mercury home whose contents are secret material. */
const CREDENTIAL_DIR_NAMES: readonly string[] = ["mcp-tokens", "browser-profile"];

/**
 * Resolve the Mercury home directories whose credential stores are denied.
 * Covers the active home (`MERCURY_HOME` / `HERMES_HOME` / `~/.mercury`) plus
 * the global Mercury root when running under a profile
 * (`<root>/profiles/<name>` also denies `<root>` stores) — same shape as the
 * hermes read guard widening.
 */
function credentialBases(): string[] {
	const homes = new Set<string>();
	for (const candidate of [process.env.MERCURY_HOME, process.env.HERMES_HOME, path.join(os.homedir(), ".mercury")]) {
		if (candidate && candidate.trim() !== "") homes.add(path.resolve(candidate));
	}
	const bases = [...homes];
	for (const home of bases) {
		const parts = home.split(path.sep);
		if (parts.length >= 3 && parts[parts.length - 2] === "profiles") {
			bases.push(parts.slice(0, -2).join(path.sep) || path.sep);
		}
	}
	return bases;
}

function isWithinOrEqual(parent: string, child: string): boolean {
	if (child === parent) return true;
	const relative = path.relative(parent, child);
	return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

/**
 * Return a location-only denial message when `absolutePath` targets a
 * secret-bearing file, or `undefined` when the read may proceed.
 *
 * Defense-in-depth, not a security boundary: the shell can still `cat` these
 * files (terminal output redaction + provider-boundary secret obfuscation are
 * the backstops). The message names only the path — never a secret value.
 */
export function getReadBlockError(absolutePath: string): string | undefined {
	const resolved = path.resolve(absolutePath);
	if (BLOCKED_ENV_BASENAMES[path.basename(resolved).toLowerCase()] === true) {
		return (
			`Access denied: '${absolutePath}' is a secret-bearing environment file ` +
			`and cannot be read to prevent credential leakage. ` +
			`If you need to check the file structure, read .env.example instead.`
		);
	}
	for (const base of credentialBases()) {
		for (const name of CREDENTIAL_FILE_NAMES) {
			if (resolved === path.join(base, name)) {
				return (
					`Access denied: '${absolutePath}' is a Mercury credential store ` +
					`and cannot be read directly. Provider tools consume these ` +
					`credentials through internal channels.`
				);
			}
		}
		for (const dir of [...CREDENTIAL_DIR_NAMES, path.join("skills", ".hub")]) {
			if (isWithinOrEqual(path.join(base, dir), resolved)) {
				return (
					`Access denied: '${absolutePath}' is inside a protected Mercury ` +
					`directory (${dir}) and cannot be read directly.`
				);
			}
		}
	}
	return undefined;
}
