/**
 * Local-only `/skills` slash command: list, show, and run the session's
 * loaded skills (the shared Mercury skills library, bridged into the omp
 * agent dir at spawn).
 *
 * This is NOT the upstream Skillshare registry flow (`search` / `install` /
 * `update` against skills.omp.sh): registry contact is excluded in Mercury,
 * so those subcommands are deliberately absent. Everything here reads the
 * in-memory skill snapshot or SKILL.md files from disk — zero network.
 */
import { buildSkillPromptMessage, getActiveSkills, type Skill } from "../extensibility/skills";
import { errorMessage, parseSubcommand } from "./helpers/parse";
import type { SlashCommandResult, SlashCommandRuntime, SlashCommandSpec, TuiSlashCommandRuntime } from "./types";

const USAGE = [
	"Local skills commands (no registry; reads the session's loaded skills):",
	"  /skills list                List loaded skills",
	"  /skills show <name>         Show a skill's SKILL.md",
	"  /skills run <name> [args]   Run a skill with optional arguments",
	"  /skills help                Show this help",
].join("\n");

/** Lines of SKILL.md body shown by `show`; beyond this, print the path instead. */
const SHOW_LINE_LIMIT = 200;

function formatSkillList(skills: readonly Skill[]): string {
	if (skills.length === 0) {
		return "No skills loaded. Skills sync from the shared library when the session starts.";
	}
	return skills.map(skill => `${skill.name} — ${skill.description} [${skill.source}]`).join("\n");
}

function findSkill(name: string): Skill | undefined {
	return getActiveSkills().find(skill => skill.name === name);
}

async function showSkill(name: string): Promise<string> {
	const skill = findSkill(name);
	if (!skill) throw new Error(`Unknown skill: ${name}`);
	const content = await Bun.file(skill.filePath).text();
	const lines = content.split("\n");
	if (lines.length <= SHOW_LINE_LIMIT) return content.trimEnd();
	return `${lines.slice(0, SHOW_LINE_LIMIT).join("\n")}\n… (${lines.length} lines total: ${skill.filePath})`;
}

/** Shared list/show/help core; `emit` targets the caller's surface. */
async function listShowHelp(verb: string, rest: string, emit: (text: string) => void | Promise<void>): Promise<void> {
	switch (verb) {
		case "":
		case "help":
			await emit(USAGE);
			return;
		case "list":
			await emit(formatSkillList(getActiveSkills()));
			return;
		case "show": {
			if (!rest) throw new Error("Usage: /skills show <name>");
			await emit(await showSkill(rest.split(/\s+/)[0]!));
			return;
		}
		default:
			throw new Error(`Unknown /skills subcommand: ${verb}\n\n${USAGE}`);
	}
}

export const BUILTIN_SKILLS_LOCAL_SLASH_COMMANDS: ReadonlyArray<SlashCommandSpec> = [
	{
		name: "skills",
		icon: "skill",
		description: "List, show, and run the session's loaded skills",
		acpDescription: "List, show, and run the session's loaded skills",
		acpInputHint: "[list|show <name>|run <name> [args]|help]",
		subcommands: [
			{ name: "list", description: "List loaded skills" },
			{ name: "show", description: "Show a skill's SKILL.md", usage: "<name>" },
			{ name: "run", description: "Run a skill with optional arguments", usage: "<name> [args]" },
			{ name: "help", description: "Show /skills help" },
		],
		allowArgs: true,
		handle: async (command, runtime: SlashCommandRuntime): Promise<SlashCommandResult> => {
			const { verb, rest } = parseSubcommand(command.args);
			if (verb === "run") {
				const [name, ...argsRest] = rest.split(/\s+/);
				if (!name) throw new Error("Usage: /skills run <name> [args]");
				const skill = findSkill(name);
				if (!skill) throw new Error(`Unknown skill: ${name}`);
				const { message } = await buildSkillPromptMessage(skill, argsRest.join(" "), "user");
				return { prompt: message };
			}
			try {
				await listShowHelp(verb, rest, runtime.output);
			} catch (error) {
				await runtime.output(`Skills: ${errorMessage(error)}`);
			}
			return undefined;
		},
		handleTui: async (command, runtime: TuiSlashCommandRuntime): Promise<SlashCommandResult> => {
			const { ctx } = runtime;
			ctx.editor.setText("");
			const { verb, rest } = parseSubcommand(command.args);
			try {
				if (verb === "run") {
					const [name, ...argsRest] = rest.split(/\s+/);
					if (!name) {
						ctx.showError("Usage: /skills run <name> [args]");
						return;
					}
					const skill = findSkill(name);
					if (!skill) {
						ctx.showError(`Unknown skill: ${name}`);
						return;
					}
					const { message } = await buildSkillPromptMessage(skill, argsRest.join(" "), "user");
					return { prompt: message };
				}
				await listShowHelp(verb, rest, text => ctx.showStatus(text));
			} catch (error) {
				ctx.showError(`Skills: ${errorMessage(error)}`);
			}
			return;
		},
	},
];
