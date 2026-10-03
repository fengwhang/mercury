import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

/** The two engines share a bank within a profile, never between profiles. */
export function mercuryProfileHome(location?: string): string | undefined {
	const root = process.env.MERCURY_HOME?.trim();
	let current = path.resolve(location || process.env.MERCURY_PROFILE_HOME || process.env.HERMES_HOME || ".");
	while (true) {
		const parent = path.dirname(current);
		const owner = path.dirname(parent);
		if (
			path.basename(parent) === "profiles" &&
			!path.basename(current).startsWith(".") &&
			((root && (owner === path.resolve(root, "hermes") || owner === path.resolve(root))) ||
				[".mercury", ".mercury-nightly"].includes(path.basename(owner)) ||
				(path.basename(owner) === "hermes" &&
					[".mercury", ".mercury-nightly"].includes(path.basename(path.dirname(owner)))))
		) {
			return current;
		}
		if (parent === current) return undefined;
		current = parent;
	}
}

/** Authored and learned skills use the same library in either engine. */
export function mercurySkillsDir(location?: string): string | undefined {
	const profile = mercuryProfileHome(location);
	if (profile) return path.join(profile, "skills");
	const root = process.env.MERCURY_HOME?.trim();
	return process.env.MERCURY_SKILLS_DIR?.trim() || (root ? path.join(root, "skills") : undefined);
}

export function mercuryProfileBankPath(configured: string | undefined, location?: string): string | undefined {
	const home = mercuryProfileHome(location);
	if (!home) return undefined;
	const fallback = path.join(home, "memories", "mnemopi.db");
	const value = configured?.trim();
	if (!value) return fallback;
	const expanded = value.startsWith("~/") ? path.join(os.homedir(), value.slice(2)) : value;
	const candidate = path.resolve(home, expanded);
	let resolved = candidate;
	try {
		resolved = fs.realpathSync(candidate);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
	const relative = path.relative(home, resolved);
	return relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) ? fallback : candidate;
}
