// Mercury: Jupyter-style message rendering — markdown + LaTeX on top of the
// existing IRC parse pipeline.
//
// Design: inline markdown maps to IRC control codes BEFORE parseStyle runs,
// so links, channels, emoji, and nick popups keep working inside formatted
// text. Fenced code blocks and display math are carved out first (single-line
// placeholders), rendered as highlight.js / KaTeX islands, and spliced back
// around parseLine output.
//
// Security: the only innerHTML sinks are KaTeX and highlight.js output, both
// of which escape their input. Raw user HTML never reaches the DOM — angle
// brackets in chat text become control codes or escaped text, never markup.

import {h as createElement, VNode} from "vue";
import katex from "katex";
import hljs from "highlight.js/lib/common";

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
	kind: "code" | "math";
	lang: string;
	body: string;
};

const placeholder = (i: number) => `${PH_OPEN}${i}${PH_CLOSE}`;

export function lookupBlock(line: string, blocks: MdBlock[]): MdBlock | null {
	const m = line.trim().match(/^\uE000(\d+)\uE001$/);

	if (!m) {
		return null;
	}

	return blocks[Number(m[1])] || null;
}

// Carve fenced code blocks and display-math out of the message. Fences win
// over math (code may contain $$); inline code spans are handled per line
// later, after math is split, so `$` inside backticks never becomes TeX.
export function extractBlocks(text: string): {text: string; blocks: MdBlock[]} {
	const blocks: MdBlock[] = [];

	// Opening fence at line start; closes at a lone ``` or end of message
	// (unclosed fence = code to the end, like Jupyter).
	text = text.replace(
		/(^|\n)```([^\n` ]*)[ \t]*\n([\s\S]*?)(?:\n```[ \t]*(?=\n|$)|$)/g,
		(match, nl: string, lang: string, body: string) =>
			`${nl === "\n" ? "\n" : ""}${placeholder(
				blocks.push({kind: "code", lang, body: body.replace(/\n$/, "")}) - 1
			)}\n`
	);

	text = text.replace(/\$\$([\s\S]+?)\$\$/g, (match, body: string) =>
		placeholder(blocks.push({kind: "math", lang: "", body}) - 1)
	);

	return {text, blocks};
}

export type BlockDesc =
	| {type: "prose"}
	| {type: "code" | "math"; block: MdBlock}
	| {type: "h"; level: number; inner: string}
	| {type: "quote"; inner: string}
	| {type: "ul" | "ol"; indent: number; inner: string}
	| {type: "hr"};

// Classify one carved line. `#channel` is safe: headings need `#` + space,
// and IRC channels never contain spaces.
export function detectBlock(line: string, blocks: MdBlock[]): BlockDesc {
	const block = lookupBlock(line, blocks);

	if (block) {
		return {type: block.kind, block};
	}

	let m = line.match(/^(#{1,6})\s+(.*)$/);

	if (m) {
		return {type: "h", level: m[1].length, inner: m[2]};
	}

	if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
		return {type: "hr"};
	}

	m = line.match(/^(?:>\s?)+(.*)$/);

	if (m) {
		return {type: "quote", inner: m[1]};
	}

	m = line.match(/^(\s*)[-*]\s+(.*)$/);

	if (m) {
		return {type: "ul", indent: m[1].length, inner: m[2]};
	}

	m = line.match(/^(\s*)(\d+)[.)]\s+(.*)$/);

	if (m) {
		return {type: "ol", indent: m[1].length, inner: m[3]};
	}

	return {type: "prose"};
}

// Split a prose line on inline `$…$`. Guards: no newlines, no flanking
// spaces (kills `$5 and $10` currency chains), backslash-escaped `\$`
// never opens. Returns alternating text/TeX pieces.
export function splitInlineMath(line: string): Array<string | {tex: string}> {
	const pieces: Array<string | {tex: string}> = [];
	const re = /(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$/g;
	let last = 0;
	let m: RegExpExecArray | null;

	while ((m = re.exec(line)) !== null) {
		if (m.index > last) {
			pieces.push(line.slice(last, m.index));
		}

		pieces.push({tex: m[1]});
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
// must never become emphasis or math. The multiline pipeline in parse.ts
// still applies — this gates markdown only, never line structure. The set
// mirrors the server's trace prefixes (hermes/observatory/rooms.py) plus
// the memory-provider glyphs; agent replies and human messages (no leading
// glyph) keep full markdown. Exported: stable cross-layer contract and the
// unit-test seam for the trace set.
export function isTraceLine(line: string): boolean {
	return /^[🔧💭ℹ️🚀✅🌀👁️🧠]/u.test(line);
}

export function renderCodeBlock(block: MdBlock): VNode {
	const lang = block.lang.toLowerCase();
	const html =
		lang && hljs.getLanguage(lang)
			? hljs.highlight(block.body, {language: lang}).value
			: escapeHtml(block.body);

	return createElement("pre", {class: "md-code"}, [
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
