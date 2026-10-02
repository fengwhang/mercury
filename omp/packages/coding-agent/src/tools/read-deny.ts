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

/**
 * Shell verbs that print file bytes (dumpers, pagers, searchers, stream
 * readers, encoders). Paired with a Mercury credential path below, the
 * segment reads secrets the `read` tool refuses — the shell must not be
 * the open door around that deny (same pairing rationale hermes uses for
 * its terminal-side sensitive-write coverage).
 */
const SHELL_READ_VERBS: readonly string[] = [
	"cat",
	"head",
	"tail",
	"less",
	"more",
	"most",
	"bat",
	"tac",
	"nl",
	"strings",
	"xxd",
	"od",
	"hexdump",
	"base64",
	"grep",
	"rg",
	"ripgrep",
	"ag",
	"ack",
	"sed",
	"awk",
];

/**
 * Shell verbs that copy a file elsewhere. `cp ~/.mercury/.env /tmp/x`
 * followed by a read of `/tmp/x` defeats the read deny without ever
 * naming a secret path in a read verb, so copy-out of a credential
 * store is denied at the copy step.
 */
const SHELL_COPYOUT_VERBS: readonly string[] = ["cp", "mv", "install", "ln", "dd", "tar", "rsync", "scp", "sftp"];

function escapeRegExp(text: string): string {
	return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Regex fragment matching a Mercury credential path as it can appear in a
 * shell command: textual home forms (`~`, `$HOME`, `$MERCURY_HOME`,
 * `$HERMES_HOME`, brace variants) plus every resolved absolute base
 * (active home, global root, profile-root widening — resolved at call
 * time, never snapshotted, so deferred home resolution can't go stale).
 * Covers the exact file list `getReadBlockError` denies plus the
 * credential directory prefixes.
 */
function shellCredentialPathPattern(cwd?: string): string {
	const lits: string[] = [
		"~/.mercury",
		"$HOME/.mercury",
		// oxlint-disable-next-line no-template-curly-in-string -- literal shell `${HOME}` form matched in commands
		"${HOME}/.mercury",
		"$MERCURY_HOME",
		// oxlint-disable-next-line no-template-curly-in-string -- literal shell `${MERCURY_HOME}` form matched in commands
		"${MERCURY_HOME}",
		"$HERMES_HOME",
		// oxlint-disable-next-line no-template-curly-in-string -- literal shell `${HERMES_HOME}` form matched in commands
		"${HERMES_HOME}",
	];
	for (const base of credentialBases()) lits.push(base);
	const files = [...CREDENTIAL_FILE_NAMES, ...CREDENTIAL_DIR_NAMES, path.join("skills", ".hub")];
	const seen = new Set<string>();
	const alts: string[] = [];
	for (const lit of lits) {
		for (const file of files) {
			const key = `${lit}\0${file}`;
			if (seen.has(key)) continue;
			seen.add(key);
			alts.push(`${escapeRegExp(lit)}/${escapeRegExp(file)}`);
		}
	}
	// Bare basenames only when the command runs inside a Mercury home
	// (`cd ~/.mercury && cat .env`): the cwd pins the meaning, so a
	// project-local `.env` elsewhere never matches.
	try {
		const dir = (cwd ?? "").trim();
		if (dir !== "" && credentialBases().some(base => isWithinOrEqual(base, path.resolve(dir)))) {
			for (const name of [...Object.keys(BLOCKED_ENV_BASENAMES), ...CREDENTIAL_FILE_NAMES]) {
				const key = `bare\0${name}`;
				if (seen.has(key)) continue;
				seen.add(key);
				alts.push(`(?:^|[\\s"'\x60;|&<>()])${escapeRegExp(path.basename(name))}`);
			}
		}
	} catch {
		// Unresolvable cwd: absolute/home forms above still apply.
	}
	return `(?:${alts.join("|")})(?=$|[\\s"'\x60;|&<>()])`;
}

/**
 * Return a denial message when a shell command reads (or copies out) a
 * Mercury credential store — the shell-layer pair to the `read`-tool
 * deny in `getReadBlockError`. Built-in and always on: unlike the
 * user-configurable `bash.patterns` / interceptor rules, this is not
 * overridable policy but a static guard, evaluated in the bash tool's
 * approval gate and execute preflight alike.
 *
 * Interpreter-internal reads (`python3 -c "open(...)"`, scripts that
 * open the file themselves) are invisible to command-shape detection
 * and remain out of scope — same documented limit as hermes'
 * terminal-side coverage, which gates verbs + paths, not syscalls.
 */
export function getShellCredentialReadBlockError(command: string, opts?: { cwd?: string }): string | undefined {
	if (!command || command.trim() === "") return undefined;
	const verbs = [...SHELL_READ_VERBS, ...SHELL_COPYOUT_VERBS];
	// Command-position anchor (hermes _CMDPOS shape): start, segment
	// separators, quotes (catches `sh -c 'cat <creds>'`), or a privilege
	// adjunct (`sudo cat <creds>`). A bare space is NOT a separator, so
	// prose (`echo dont cat ~/.mercury/.env`) never matches.
	const verbPattern = new RegExp(
		`(?:^|[;|&(\x60$"'\n]|\\b(?:sudo|doas|su|runuser|env|command|builtin)\\s+)\\s*(?:${verbs.join("|")})(?=\\s|$)`,
		"im",
	);
	if (!verbPattern.test(command)) return undefined;
	const pathPattern = new RegExp(shellCredentialPathPattern(opts?.cwd));
	const hit = pathPattern.exec(command);
	if (!hit) return undefined;
	return (
		`Access denied: shell commands cannot read Mercury credential stores ` +
		`('${hit[0].trim()}' is denied the same way it is for the read tool). ` +
		`Provider tools consume these credentials through internal channels; ` +
		`ask the user if you need a value from it.`
	);
}
