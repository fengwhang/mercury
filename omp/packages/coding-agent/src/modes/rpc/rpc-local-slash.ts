import {
	BUILTIN_SLASH_COMMANDS_INTERNAL,
	lookupBuiltinSlashCommand,
} from "../../slash-commands/builtin-registry";
import { parseSlashCommand } from "../../slash-commands/helpers/parse";

/**
 * Local answers for known slash commands with no headless implementation.
 *
 * The RPC prompt pipeline runs skill prompts, then ACP builtins (specs
 * with a `handle`), then falls through to the agent turn. Commands with
 * neither — mostly interactive/TUI-only verbs like `/help` or `/clear` —
 * used to fall into the agent turn as plain text, and the model would
 * hallucinate compliance ("entering fast mode"). Anything this function
 * answers never reaches the model.
 *
 * Returns the reply text, or null when the engine should handle the
 * input normally (plain chat, unknown commands, specs with a `handle`).
 */
export function localSlashResponse(text: string): string | null {
	const parsed = parseSlashCommand(text);
	if (!parsed) return null;
	const name = parsed.name.toLowerCase();
	if (name === "help") {
		// No top-level help spec exists (help is TUI-rendered) — answer
		// from the registry directly, never the model.
		const topic = parsed.args.trim().toLowerCase();
		if (topic) {
			const topicSpec = lookupBuiltinSlashCommand(topic);
			if (!topicSpec) {
				return `No such command: /${topic}. Send /help for the list.`;
			}
			const blurb = topicSpec.acpDescription ?? topicSpec.description ?? "";
			const usage = "usage" in topicSpec && typeof topicSpec.usage === "string"
				? topicSpec.usage
				: "";
			return usage ? `/${topicSpec.name} — ${blurb}\nUsage: ${usage}` : `/${topicSpec.name} — ${blurb}`;
		}
		const lines = BUILTIN_SLASH_COMMANDS_INTERNAL.filter(spec =>
			/^[a-z0-9-]+$/.test(spec.name),
		).map(spec => {
			const blurb = spec.acpDescription ?? spec.description ?? "";
			return blurb ? `/${spec.name} — ${blurb}` : `/${spec.name}`;
		}).sort();
		return `Available commands:\n${lines.join("\n")}`;
	}
	const spec = lookupBuiltinSlashCommand(name) ?? lookupBuiltinSlashCommand(parsed.name);
	if (!spec || typeof spec.handle === "function") {
		return null;
	}
	return `/${name} is interactive and can't run over this channel.`;
}
