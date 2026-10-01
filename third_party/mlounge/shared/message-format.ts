// Mercury rendering provenance is independent of the IRC MESSAGE/NOTICE type.
export const mercuryKindTag = "+mercury/kind";
export const mercuryMessageKinds = [
	"assistant_reply",
	"user",
	"tool_input",
	"tool_output",
	"thinking",
	"status",
] as const;
export type MercuryMessageKind = typeof mercuryMessageKinds[number];

export function mercuryMessageKind(value: unknown): MercuryMessageKind | undefined {
	return mercuryMessageKinds.includes(value as MercuryMessageKind)
		? (value as MercuryMessageKind)
		: undefined;
}
