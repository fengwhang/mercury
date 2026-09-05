/**
 * Agent Dirs (.agent/.agents) Provider
 *
 * Loads skills, rules, prompts, commands, context files, and system prompts
 * from .agent/ and .agents/ directories at both user (~/) and project levels.
 * Project-level discovery walks up from cwd to repoRoot.
 */
import * as path from "node:path";
import { isWsl, windowsPathToWslMount } from "@oh-my-pi/pi-utils";
import { registerProvider } from "../capability";
import { type ContextFile, contextFileCapability } from "../capability/context-file";
import { readFile } from "../capability/fs";
import { type Prompt, promptCapability } from "../capability/prompt";
import { type Rule, ruleCapability } from "../capability/rule";
import { type Skill, skillCapability } from "../capability/skill";
import { type SlashCommand, slashCommandCapability } from "../capability/slash-command";
import { type SystemPrompt, systemPromptCapability } from "../capability/system-prompt";
import type { LoadContext, LoadResult } from "../capability/types";
import {
	buildRuleFromMarkdown,
	calculateDepth,
	createSourceMeta,
	loadFilesFromDir,
	scanSkillsFromDir,
} from "./helpers";

const PROVIDER_ID = "agents";
const DISPLAY_NAME = "Agent Dirs (.agent/.agents)";
const PRIORITY = 70;
const AGENT_DIR_CANDIDATES = [".agent", ".agents"] as const;

interface UserPathCandidateOptions {
	platform?: NodeJS.Platform;
	env?: NodeJS.ProcessEnv;
	windowsUserProfile?: () => string | undefined;
	wslPath?: (windowsPath: string) => string | undefined;
}

/**
 * Hard cap for best-effort host-discovery probes.
 *
 * WSL→Windows interop can wedge indefinitely (issue #8402): a synchronous
 * spawn with no timeout blocks the whole startup thread before the TUI paints
 * or any log file is created. The probe result only ever augments discovery
 * with an extra host-home candidate, so a few hundred milliseconds is a
 * generous ceiling — past it we treat the host as unavailable.
 */
const HOST_PROBE_TIMEOUT_MS = 500;

/**
 * Run a best-effort discovery probe and return its trimmed stdout, or
 * `undefined` when the command fails, produces no output, or exceeds the
 * timeout. On timeout the child is killed with SIGKILL so a wedged interop pipe
 * cannot hang startup; the killed/non-zero exit is then reported as
 * "unavailable" and discovery falls back to the Linux `$HOME`/`~/.omp`
 * candidates.
 */
export function runHostProbe(cmd: string[], timeoutMs = HOST_PROBE_TIMEOUT_MS): string | undefined {
	try {
		const result = Bun.spawnSync(cmd, {
			stdout: "pipe",
			stderr: "ignore",
			timeout: timeoutMs,
			killSignal: "SIGKILL",
		});
		if (result.exitCode !== 0) return undefined;
		const resolved = result.stdout.toString().trim();
		return resolved.length > 0 ? resolved : undefined;
	} catch {
		return undefined;
	}
}

function resolveWithWslPath(windowsPath: string): string | undefined {
	return runHostProbe(["wslpath", "-u", windowsPath]);
}

function resolveWindowsUserProfile(): string | undefined {
	const resolved = runHostProbe(["cmd.exe", "/d", "/c", "echo", "%USERPROFILE%"]);
	return resolved && resolved !== "%USERPROFILE%" ? resolved : undefined;
}

/** Resolve the Windows host profile home exposed to WSL, if available. */
export function getWslWindowsHomeCandidate(options: UserPathCandidateOptions = {}): string | undefined {
	const platform = options.platform ?? process.platform;
	const env = options.env ?? process.env;
	if (!isWsl(platform, env)) return undefined;
	const userProfile = env.USERPROFILE ?? (options.windowsUserProfile ?? resolveWindowsUserProfile)();
	if (!userProfile) return undefined;
	const interopPath = (options.wslPath ?? resolveWithWslPath)(userProfile);
	if (interopPath !== undefined) return interopPath;
	const trimmed = userProfile.trim();
	return path.posix.isAbsolute(trimmed) ? path.posix.normalize(trimmed) : windowsPathToWslMount(trimmed);
}

/**
 * Memo for the default-probe WSL home resolution, keyed by the inputs that
 * decide it (platform + WSL markers + `USERPROFILE`). Discovery calls
 * {@link getUserPathCandidates} from every loader (skills, rules, prompts,
 * commands, AGENTS.md, SYSTEM.md); the host-home probe spawns `cmd.exe` over
 * the WSL interop pipe, so without the memo a wedged pipe costs one
 * {@link HOST_PROBE_TIMEOUT_MS} stall per loader. Keying by inputs keeps
 * test/SDK environment changes visible instead of pinning the first answer
 * for the process lifetime.
 */
const wslHomeMemo = new Map<string, string | undefined>();

function getUserHomeCandidates(ctx: LoadContext): string[] {
	const homes = [ctx.home];
	const env = process.env;
	const key = `${process.platform}\0${env.WSL_DISTRO_NAME ?? ""}\0${env.WSL_INTEROP ?? ""}\0${env.USERPROFILE ?? ""}`;
	let wslHome: string | undefined;
	if (wslHomeMemo.has(key)) {
		wslHome = wslHomeMemo.get(key);
	} else {
		wslHome = getWslWindowsHomeCandidate();
		wslHomeMemo.set(key, wslHome);
	}
	if (wslHome && !homes.includes(wslHome)) homes.push(wslHome);
	return homes;
}

/** User-level paths: ~/.agent[s]/<segments>, plus the Windows host profile under WSL. */
export function getUserPathCandidates(ctx: LoadContext, ...segments: string[]): string[] {
	return getUserHomeCandidates(ctx).flatMap(home =>
		AGENT_DIR_CANDIDATES.map(baseDir => path.join(home, baseDir, ...segments)),
	);
}

/**
 * Project-level paths: walk up from cwd to repoRoot, returning `.agent/<segments>`
 * and `.agents/<segments>` at each ancestor.
 *
 * The user home directory is skipped: `~/.agent[s]/` is by definition
 * user-level config and is already enumerated by {@link getUserPathCandidates}.
 * Without this guard, any cwd under `$HOME` (with no closer git repoRoot) would
 * walk up to home and yield duplicate project+user entries for the same
 * directory — see https://github.com/can1357/oh-my-pi/issues/1116.
 */
export function getProjectPathCandidates(ctx: LoadContext, ...segments: string[]): string[] {
	const paths: string[] = [];
	let current = ctx.cwd;
	while (true) {
		if (current !== ctx.home) {
			for (const baseDir of AGENT_DIR_CANDIDATES) {
				paths.push(path.join(current, baseDir, ...segments));
			}
		}
		if (current === (ctx.repoRoot ?? ctx.home)) break;
		const parent = path.dirname(current);
		if (parent === current) break;
		current = parent;
	}
	return paths;
}

// Skills
async function loadSkills(ctx: LoadContext): Promise<LoadResult<Skill>> {
	const projectScans = getProjectPathCandidates(ctx, "skills").map(dir =>
		scanSkillsFromDir(ctx, { dir, providerId: PROVIDER_ID, level: "project" }),
	);
	const userScans = getUserPathCandidates(ctx, "skills").map(dir =>
		scanSkillsFromDir(ctx, { dir, providerId: PROVIDER_ID, level: "user" }),
	);

	// HERMES-OMP PATCH (shared skills, REVISED per user decision): the
	// mercury home IS the user home under MERCURY_HOME (capability/index.ts
	// ctx patch), so getUserPathCandidates above already resolves
	// $MERCURY_HOME/.agent(s)/skills natively. The FLAT shared library
	// ($MERCURY_HOME/skills — hermes' native layout) rides FIRST in the
	// user scan set: one pass, zero add-on providers, both engines point
	// at the same tree.
	const mercuryHome = process.env.MERCURY_HOME?.trim();
	const sharedDir = mercuryHome ? [path.join(mercuryHome, "skills")] : [];
	const results = await Promise.all([...projectScans, ...sharedDir.map(dir =>
		scanSkillsFromDir(ctx, { dir, providerId: PROVIDER_ID, level: "user" })),
		...userScans]);

	return {
		items: results.flatMap(r => r.items),
		warnings: results.flatMap(r => r.warnings ?? []),
	};
}

registerProvider<Skill>(skillCapability.id, {
	id: PROVIDER_ID,
	displayName: DISPLAY_NAME,
	description: "Load skills from .agent/skills and .agents/skills (project walk-up + user home)",
	priority: PRIORITY,
	load: loadSkills,
});

// Rules
async function loadRules(ctx: LoadContext): Promise<LoadResult<Rule>> {
	const load = (dir: string, level: "user" | "project") =>
		loadFilesFromDir<Rule>(ctx, dir, PROVIDER_ID, level, {
			extensions: ["md", "mdc"],
			transform: (name, content, filePath, source) =>
				buildRuleFromMarkdown(name, content, filePath, source, { stripNamePattern: /\.(md|mdc)$/ }),
		});

	const results = await Promise.all([
		...getProjectPathCandidates(ctx, "rules").map(dir => load(dir, "project")),
		...getUserPathCandidates(ctx, "rules").map(dir => load(dir, "user")),
	]);

	return {
		items: results.flatMap(r => r.items),
		warnings: results.flatMap(r => r.warnings ?? []),
	};
}

registerProvider<Rule>(ruleCapability.id, {
	id: PROVIDER_ID,
	displayName: DISPLAY_NAME,
	description: "Load rules from .agent/rules and .agents/rules (project walk-up + user home)",
	priority: PRIORITY,
	load: loadRules,
});

// Prompts
async function loadPrompts(ctx: LoadContext): Promise<LoadResult<Prompt>> {
	const load = (dir: string, level: "user" | "project") =>
		loadFilesFromDir<Prompt>(ctx, dir, PROVIDER_ID, level, {
			extensions: ["md"],
			transform: (name, content, filePath, source) => ({
				name: name.replace(/\.md$/, ""),
				path: filePath,
				content,
				_source: source,
			}),
		});

	const results = await Promise.all([
		...getProjectPathCandidates(ctx, "prompts").map(dir => load(dir, "project")),
		...getUserPathCandidates(ctx, "prompts").map(dir => load(dir, "user")),
	]);

	return {
		items: results.flatMap(r => r.items),
		warnings: results.flatMap(r => r.warnings ?? []),
	};
}

registerProvider<Prompt>(promptCapability.id, {
	id: PROVIDER_ID,
	displayName: DISPLAY_NAME,
	description: "Load prompts from .agent/prompts and .agents/prompts (project walk-up + user home)",
	priority: PRIORITY,
	load: loadPrompts,
});

// Slash Commands
async function loadSlashCommands(ctx: LoadContext): Promise<LoadResult<SlashCommand>> {
	const load = (dir: string, level: "user" | "project") =>
		loadFilesFromDir<SlashCommand>(ctx, dir, PROVIDER_ID, level, {
			extensions: ["md"],
			transform: (name, content, filePath, source) => ({
				name: name.replace(/\.md$/, ""),
				path: filePath,
				content,
				level,
				_source: source,
			}),
		});

	const results = await Promise.all([
		...getProjectPathCandidates(ctx, "commands").map(dir => load(dir, "project")),
		...getUserPathCandidates(ctx, "commands").map(dir => load(dir, "user")),
	]);

	return {
		items: results.flatMap(r => r.items),
		warnings: results.flatMap(r => r.warnings ?? []),
	};
}

registerProvider<SlashCommand>(slashCommandCapability.id, {
	id: PROVIDER_ID,
	displayName: DISPLAY_NAME,
	description: "Load commands from .agent/commands and .agents/commands (project walk-up + user home)",
	priority: PRIORITY,
	load: loadSlashCommands,
});

// Context Files (AGENTS.md)
async function loadContextFiles(ctx: LoadContext): Promise<LoadResult<ContextFile>> {
	const load = async (filePath: string, level: "user" | "project"): Promise<ContextFile | null> => {
		const content = await readFile(filePath);
		if (!content) return null;
		// filePath is <ancestor>/.agent(s)/AGENTS.md — go up past the config dir to the ancestor
		const ancestorDir = path.dirname(path.dirname(filePath));
		const depth = level === "project" ? calculateDepth(ctx.cwd, ancestorDir, path.sep) : undefined;
		return { path: filePath, content, level, depth, _source: createSourceMeta(PROVIDER_ID, filePath, level) };
	};

	const results = await Promise.all([
		...getProjectPathCandidates(ctx, "AGENTS.md").map(p => load(p, "project")),
		...getUserPathCandidates(ctx, "AGENTS.md").map(p => load(p, "user")),
	]);

	return { items: results.filter((r): r is ContextFile => r !== null), warnings: [] };
}

// HERMES-OMP PATCH: OMP.md — harness-scoped prompt file for the omp half of
// Mercury. Loaded ONLY into omp's context (system prompt tier); hermes never
// sees it (hermes' counterpart is HERMES.md via load_hermes_md_home).
// Optional: absent file → no items. Lives at MERCURY_HOME top level
// (~/.mercury/OMP.md), mirroring SOUL/MEMORY/USER in the shared layout.
async function loadMercuryOmpMd(_ctx: LoadContext): Promise<LoadResult<ContextFile>> {
	const mercuryHome = process.env.MERCURY_HOME?.trim();
	if (!mercuryHome) return { items: [], warnings: [] };
	// MERCURY LAYOUT: config/ first, top-level fallback.
	// PROFILE COMPOSITION (user directive): the spawning profile's OMP.md
	// overrides the shared one — same mechanics as the memory composite.
	const profileHome = process.env.MERCURY_PROFILE_HOME?.trim();
	const filePath =
		(profileHome && (await readFile(path.join(profileHome, "config", "OMP.md"))))
			? path.join(profileHome, "config", "OMP.md")
			: (await readFile(path.join(mercuryHome, "config", "OMP.md")))
				? path.join(mercuryHome, "config", "OMP.md")
				: path.join(mercuryHome, "OMP.md");
	const content = await readFile(filePath);
	if (!content) return { items: [], warnings: [] };
	return {
		items: [{ path: filePath, content, level: "user", depth: undefined, _source: createSourceMeta("mercury-omp-md", filePath, "user") }],
		warnings: [],
	};
}

registerProvider<ContextFile>(contextFileCapability.id, {
	id: "mercury-omp-md",
	displayName: "OMP.md (Mercury)",
	description: "Load OMP.md from the Mercury home — omp-only system prompt context (user decision 2026-09-04)",
	priority: PRIORITY,
	load: loadMercuryOmpMd,
});

// HERMES-OMP PATCH (F0/F3): shared memory files — SOUL.md / MEMORY.md /
// USER.md (plus AGENTS.md) at the Mercury home top level. ONE memory layer for both engines:
// hermes reads/writes them natively (get_memory_dir / load_soul_md patched);
// omp READS them here as user-level context files. Write discipline:
// hermes-owned — omp consumes, never writes.
async function loadMercurySharedMemory(_ctx: LoadContext): Promise<LoadResult<ContextFile>> {
	const mercuryHome = process.env.MERCURY_HOME?.trim();
	if (!mercuryHome) return { items: [], warnings: [] };
	const items: ContextFile[] = [];
	// HERMES-OMP PATCH (config/ reorg): AGENTS.md rides the same shared
	// layer — cross-project WORKING INSTRUCTIONS for both engines (distinct
	// from SOUL.md persona and the per-side HERMES/OMP.md supplements).
	// Project-level AGENTS.md (cwd walk) still loads additively upstream.
	// AUDIT FIX (2026-09-05): the context-files capability dedupes by scope —
	// key "user" admits exactly ONE user-level context file. Returning four
	// files made three of them lose the race silently (MEMORY.md never
	// reached omp in one-shot runs — the shared-memory audit caught it).
	// Fix: emit ONE composite file. Every shared layer is guaranteed to
	// arrive; ordering is explicit (persona → state → working rules).
	// MERCURY LAYOUT: shared .md files live in $MERCURY_HOME/config/; the
	// top level is the pre-layout fallback (one release).
	// PROFILE COMPOSITION (user directive): when the spawning hermes agent
	// ran under a secondary profile, MERCURY_PROFILE_HOME points at that
	// profile's home — the SAME profile mechanics composed into omp's
	// prompt. The profile's config/<name> OVERRIDES the shared file of the
	// same name; names absent from the profile fall through to shared.
	// Nested subagents inherit this through env passthrough.
	const sharedCfg = path.join(mercuryHome, "config");
	const profileCfg = process.env.MERCURY_PROFILE_HOME?.trim()
		? path.join(process.env.MERCURY_PROFILE_HOME.trim(), "config")
		: null;
	const names = ["SOUL.md", "MEMORY.md", "USER.md", "AGENTS.md"] as const;
	const parts: string[] = [];
	for (const name of names) {
		let content: string | undefined;
		if (profileCfg) content = await readFile(path.join(profileCfg, name));
		if (!content) content = await readFile(path.join(sharedCfg, name));
		if (!content) content = await readFile(path.join(mercuryHome, name));
		if (content) parts.push(content.trimEnd());
	}
	if (!parts.length) return { items: [], warnings: [] };
	items.push({
		path: path.join(mercuryHome, ".mercury-shared-context"),
		content: parts.join("\n\n---\n\n") + "\n",
		level: "user",
		depth: undefined,
		_source: createSourceMeta("mercury-memory", path.join(sharedCfg, "SOUL.md"), "user"),
	});
	return { items, warnings: [] };
}

registerProvider<ContextFile>(contextFileCapability.id, {
	id: "mercury-memory",
	displayName: "Mercury shared memory (SOUL/MEMORY/USER)",
	description: "Read the shared hermes-owned memory files (SOUL.md, MEMORY.md, USER.md, AGENTS.md) from the Mercury config dir (omp reads; hermes writes)",
	priority: PRIORITY,
	load: loadMercurySharedMemory,
});

registerProvider<ContextFile>(contextFileCapability.id, {
	id: PROVIDER_ID,
	displayName: DISPLAY_NAME,
	description: "Load AGENTS.md from .agent and .agents (project walk-up + user home)",
	priority: PRIORITY,
	load: loadContextFiles,
});

// System Prompt (SYSTEM.md)
async function loadSystemPrompt(ctx: LoadContext): Promise<LoadResult<SystemPrompt>> {
	const load = async (filePath: string, level: "user" | "project"): Promise<SystemPrompt | null> => {
		const content = await readFile(filePath);
		if (!content) return null;
		return { path: filePath, content, level, _source: createSourceMeta(PROVIDER_ID, filePath, level) };
	};

	const results = await Promise.all([
		...getProjectPathCandidates(ctx, "SYSTEM.md").map(p => load(p, "project")),
		...getUserPathCandidates(ctx, "SYSTEM.md").map(p => load(p, "user")),
	]);

	return { items: results.filter((r): r is SystemPrompt => r !== null), warnings: [] };
}

registerProvider<SystemPrompt>(systemPromptCapability.id, {
	id: PROVIDER_ID,
	displayName: DISPLAY_NAME,
	description: "Load SYSTEM.md from .agent and .agents (project walk-up + user home)",
	priority: PRIORITY,
	load: loadSystemPrompt,
});
