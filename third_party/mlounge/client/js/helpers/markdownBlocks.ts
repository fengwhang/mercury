// CommonMark blocks and pipe tables become Vue nodes, never raw HTML. Inline
// text still uses Mercury's IRC/Markdown/TeX pipeline so room links, nicknames,
// and literal commands retain their existing behavior.
import MarkdownIt from "markdown-it";
import {h, type VNode} from "vue";
import {renderCodeBlock, renderMathDisplay} from "./markdown";

type Nodes = Array<VNode | string>;
type InlineRenderer = (text: string) => Nodes;

const markdown = new MarkdownIt({html: false});
markdown.core.ruler.enableOnly(["normalize", "block"]);

// Display math is a block even without blank lines around it. Register it as
// a paragraph/list/quote terminator; fenced and indented code take precedence.
markdown.block.ruler.before(
	"paragraph",
	"mercury_math",
	(state, startLine, endLine, silent) => {
		if (state.sCount[startLine] - state.blkIndent >= 4) {
			return false;
		}

		const line = state.src.slice(
			state.bMarks[startLine] + state.tShift[startLine],
			state.eMarks[startLine]
		);
		const open = line.match(/^(\$\$|\\\[)/)?.[0];

		if (!open) {
			return false;
		}

		const end = open === "$$" ? /(?<!\\)\$\$\s*$/ : /\\\]\s*$/;
		let closeLine = startLine;
		let rest = line.slice(open.length);

		while (!end.test(rest)) {
			closeLine++;

			if (closeLine >= endLine || state.sCount[closeLine] < state.blkIndent) {
				return false;
			}

			rest = state.src.slice(
				state.bMarks[closeLine] + state.tShift[closeLine],
				state.eMarks[closeLine]
			);
		}

		if (silent) {
			return true;
		}

		const token = state.push("mercury_math", "", 0);
		token.content = state
			.getLines(startLine, closeLine + 1, state.sCount[startLine], false)
			.trim()
			.slice(open.length)
			.replace(end, "");
		token.map = [startLine, closeLine + 1];
		state.line = closeLine + 1;
		return true;
	},
	{alt: ["paragraph", "blockquote", "list"]}
);

const classes: Record<string, string> = {
	bullet_list_open: "md-ul",
	ordered_list_open: "md-ol",
	blockquote_open: "md-quote",
	paragraph_open: "md-paragraph",
	table_open: "md-table",
};

export default function renderMarkdownBlocks(text: string, inline: InlineRenderer): Nodes {
	const tokens = markdown.parse(text, {});

	// Ordinary chat retains its exact whitespace and per-line IRC parsing.
	if (tokens.length === 0 || (tokens.length === 3 && tokens[0].type === "paragraph_open")) {
		return inline(text);
	}

	const root: Nodes = [];
	const stack: Array<{token: MarkdownIt.Token; children: Nodes}> = [];
	const current = () => stack.at(-1)?.children ?? root;

	for (const token of tokens) {
		if (token.nesting === 1) {
			stack.push({token, children: []});
		} else if (token.nesting === -1) {
			const frame = stack.pop()!;
			const open = frame.token;

			if (open.hidden) {
				current().push(...frame.children);
				continue;
			}

			const props: {class?: string; start?: number; style?: {textAlign: string}} = {
				class: open.type === "heading_open" ? `md-${open.tag}` : classes[open.type],
			};
			const start = open.attrGet("start");
			const style = open.attrGet("style");
			const align =
				typeof style === "string"
					? style.match(/^text-align:(left|center|right)$/)?.[1]
					: undefined;

			if (open.type === "ordered_list_open" && start) {
				props.start = Number(start);
			}

			if (align) {
				props.style = {textAlign: align};
			}

			const node = h(open.tag, props, frame.children);
			current().push(
				open.type === "table_open" ? h("div", {class: "md-table-scroll"}, [node]) : node
			);
		} else if (token.type === "inline") {
			current().push(...inline(token.content));
		} else if (token.type === "fence" || token.type === "code_block") {
			current().push(
				renderCodeBlock({
					kind: "code",
					lang: token.info.trim().split(/\s+/)[0],
					// The parser includes the newline immediately before the closing
					// fence. Exclude that delimiter newline from copied commands.
					body: token.content.replace(/\n$/, ""),
				})
			);
		} else if (token.type === "mercury_math") {
			current().push(renderMathDisplay({kind: "math", lang: "", body: token.content}));
		} else if (token.type === "hr") {
			current().push(h("hr", {class: "md-hr"}));
		}
	}

	return root;
}
