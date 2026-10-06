import { getTinyModelsCacheDir } from "@oh-my-pi/pi-utils";
import chalk from "@oh-my-pi/pi-utils/chalk";
import {
	DEFAULT_TINY_TITLE_LOCAL_MODEL_KEY,
	isTinyLocalModelKey,
	TINY_LOCAL_MODELS,
	type TinyLocalModelKey,
} from "../tiny/models";

export type TinyModelsAction = "download" | "list";
export interface TinyModelsCommandArgs {
	action: TinyModelsAction;
	model?: string;
	flags: { json?: boolean };
}

export function resolveModels(model: string | undefined): TinyLocalModelKey[] {
	if (!model) return [DEFAULT_TINY_TITLE_LOCAL_MODEL_KEY];
	if (model === "all") return TINY_LOCAL_MODELS.filter(spec => !("unsupportedReason" in spec) || !spec.unsupportedReason).map(spec => spec.key);
	if (!isTinyLocalModelKey(model)) throw new Error(`Unknown tiny local model: ${model}. Expected one of: ${TINY_LOCAL_MODELS.map(spec => spec.key).join(", ")}, all`);
	return [model];
}

export async function runTinyModelsCommand(command: TinyModelsCommandArgs): Promise<void> {
	if (command.action === "list") {
		if (command.flags.json) process.stdout.write(`${JSON.stringify({ models: TINY_LOCAL_MODELS })}\n`);
		else {
			process.stdout.write(`${chalk.bold("Tiny local models")}\n`);
			for (const spec of TINY_LOCAL_MODELS) {
				const defaultMark = spec.key === DEFAULT_TINY_TITLE_LOCAL_MODEL_KEY ? chalk.cyan(" default") : "";
				process.stdout.write(`${chalk.cyan(spec.key)}${defaultMark}\n  ${spec.label} — ${spec.description}\n`);
			}
		}
		return;
	}
	const error = `Model downloads are disabled. Provision complete local assets under ${getTinyModelsCacheDir()}/<repo> (config, tokenizer, ONNX weights and external data), or configure a provider.`;
	const results = resolveModels(command.model).map(model => ({ model, ok: false, error }));
	if (command.flags.json) process.stdout.write(`${JSON.stringify({ results })}\n`);
	throw new Error(error);
}
