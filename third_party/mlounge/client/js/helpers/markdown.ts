// Mercury inline Markdown, literal code and KaTeX rendering. Block structure
// is handled by markdownBlocks.ts; inline emphasis reuses IRC styles to retain
// clickable room links and nicknames. Only highlight.js and KaTeX may supply
// generated HTML. User text always enters the DOM as escaped Vue text.

import {h as createElement, VNode} from "vue";
import katex from "katex";
import hljs from "highlight.js/lib/common";
import CopyButton from "../../components/CopyButton.vue";

// IRC control codes understood by parseStyle (ircmessageparser/parseStyle).
const BOLD = "\u0002";
const ITALIC = "\u001d";
const STRIKE = "\u001e";
const CODE = "\u0011";

// Private-use placeholders. PUA text cannot form links, channels, emoji, or
// nicks, so placeholders survive parseLine untouched. A user typing literal
// PUA chars can only restyle their own message — no script sink exists here.
const PH_OPEN = "\uE000";
const PH_CLOSE = "\uE001";
const ESC = "\uE002";

export type MdBlock = {
	kind: "code" | "math" | "inline-code";
	lang: string;
	body: string;
};

const placeholder = (i: number) => `${PH_OPEN}${i}${PH_CLOSE}`;

function isEscaped(text: string, index: number): boolean {
	let slashes = 0;

	while (index > 0 && text[--index] === "\\") {
		slashes++;
	}

	return slashes % 2 === 1;
}

// Matching backtick runs protect the entire literal before any math or
// emphasis parser sees it. Longer runs allow backticks inside commands.
export function splitInlineCode(text: string): Array<string | {code: string}> {
	const pieces: Array<string | {code: string}> = [];
	const runs = Array.from(text.matchAll(/`+/g));
	let last = 0;

	for (let i = 0; i < runs.length; i++) {
		const open = runs[i];

		if (isEscaped(text, open.index!)) {
			continue;
		}

		let close = i + 1;

		while (close < runs.length && runs[close][0].length !== open[0].length) {
			close++;
		}

		if (close === runs.length) {
			continue;
		}

		if (open.index! > last) {
			pieces.push(text.slice(last, open.index));
		}

		pieces.push({code: text.slice(open.index! + open[0].length, runs[close].index)});
		last = runs[close].index! + runs[close][0].length;
		i = close;
	}

	if (last < text.length) {
		pieces.push(text.slice(last));
	}

	return pieces;
}

// Protect inline code before extracting display math embedded in prose.
// Block fences are consumed by the block parser, including nested fences.
export function extractBlocks(text: string): {text: string; blocks: MdBlock[]} {
	const blocks: MdBlock[] = [];
	text = splitInlineCode(text)
		.map((piece) =>
			typeof piece === "string"
				? piece
				: placeholder(blocks.push({kind: "inline-code", lang: "", body: piece.code}) - 1)
		)
		.join("");
	text = text.replace(
		/(?<!\\)\$\$([\s\S]+?)(?<!\\)\$\$|\\\[([\s\S]+?)\\\]/g,
		(_match, dollar: string, bracket: string) =>
			placeholder(blocks.push({kind: "math", lang: "", body: dollar ?? bracket}) - 1)
	);

	return {text, blocks};
}

// Split a prose line on inline `$…$`. Guards: no newlines, no flanking
// spaces (kills `$5 and $10` currency chains), backslash-escaped `\$`
// never opens. Returns alternating text/TeX pieces.
export function splitInlineMath(line: string): Array<string | {tex: string}> {
	const pieces: Array<string | {tex: string}> = [];
	const re = /\\\(([^\n]+?)\\\)|(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$/g;
	let last = 0;
	let m: RegExpExecArray | null;

	while ((m = re.exec(line)) !== null) {
		if (m.index > last) {
			pieces.push(line.slice(last, m.index));
		}

		pieces.push({tex: m[1] ?? m[2]});
		last = m.index + m[0].length;
	}

	if (last < line.length) {
		pieces.push(line.slice(last));
	}

	return pieces;
}

// Markdown inline syntax -> IRC control codes. Runs on prose pieces only
// (math already split out). `__bold__` / `_italic_` variants deliberately
// unsupported: snake_case identifiers would false-positive.
export function inlineMdToIrcCodes(text: string): string {
	// Backslash escapes first, so \* \` \$ \\ \~ never trigger syntax.
	text = text.replace(/\\([\\`*${}~])/g, `${ESC}$1`);

	// Inline code spans out before emphasis (code may contain * etc.).
	const spans: string[] = [];
	text = text.replace(
		/`([^`\n]+)`/g,
		(match, body: string) => `${PH_OPEN}c${spans.push(body) - 1}${PH_CLOSE}`
	);

	text = text.replace(/\*\*(.+?)\*\*/g, `${BOLD}$1${BOLD}`);
	// Intra-word * stays literal (2*3*4 is math, not emphasis).
	text = text.replace(/(^|[^\w*])\*([^*\s][^*]*?)\*/g, `$1${ITALIC}$2${ITALIC}`);
	text = text.replace(/~~([^~]+?)~~/g, `${STRIKE}$1${STRIKE}`);

	text = text.replace(/\uE000c(\d+)\uE001/g, (match, i: string) => {
		const body = spans[Number(i)] ?? "";
		return `${CODE}${body}${CODE}`;
	});

	return text.replace(/\uE002([\\`*${}~])/g, "$1");
}

function escapeHtml(s: string): string {
	return s
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

// Agent execution traces (tool calls, thinking, lifecycle, memory notices)
// bypass the markdown pipeline: underscores, asterisks and $…$ in tool I/O
// must never become emphasis or math. Trace rendering preserves the original
// text and whitespace without running any formatter. The set
// mirrors the server's trace prefixes (hermes/observatory/rooms.py) plus
// the memory-provider glyphs; agent replies and human messages (no leading
// glyph) keep full markdown. Exported: stable cross-layer contract and the
// unit-test seam for the trace set.
export function isTraceLine(line: string): boolean {
	return ["🔧", "💭", "ℹ", "🚀", "✅", "🌀", "👁", "🧠"].some((prefix) => line.startsWith(prefix));
}

export function renderCodeBlock(block: MdBlock): VNode {
	const lang = block.lang.toLowerCase();
	const html =
		lang && hljs.getLanguage(lang)
			? hljs.highlight(block.body, {language: lang}).value
			: escapeHtml(block.body);

	return createElement("pre", {class: "md-code"}, [
		createElement(CopyButton, {text: block.body, label: "Copy code"}),
		createElement("code", {
			class: lang ? `language-${lang}` : "",
			innerHTML: html,
		}),
	]);
}

export function renderMathDisplay(block: MdBlock): VNode {
	return createElement("div", {
		class: "md-math-display",
		innerHTML: katex.renderToString(block.body, {
			displayMode: true,
			throwOnError: false,
			strict: false,
		}),
	});
}

export function renderMathSpan(tex: string): VNode {
	return createElement("span", {
		class: "md-math",
		innerHTML: katex.renderToString(tex, {
			displayMode: false,
			throwOnError: false,
			strict: false,
		}),
	});
}
