// @vitest-environment jsdom
// Mercury: Jupyter-style rendering — markdown inline syntax maps to IRC
// styling, fenced blocks become highlighted code, $…$ / $$…$$ become KaTeX.
import {expect, describe, it, vi} from "vitest";
vi.mock("../../../../client/js/socket", () => ({default: {}}));
import parse from "../../../../client/js/helpers/parse";

type VNode = {
	type?: unknown;
	props?: {class?: unknown; innerHTML?: unknown} | null;
	children?: unknown;
};

function eachNode(nodes: unknown, visit: (node: VNode | string) => void): void {
	const list = (Array.isArray(nodes) ? nodes : [nodes]) as Array<VNode | string>;

	for (const node of list) {
		if (Array.isArray(node)) {
			eachNode(node, visit);
		} else {
			visit(node);
		}
	}
}

function classes(nodes: unknown): string[] {
	const out: string[] = [];
	eachNode(nodes, (node) => {
		if (typeof node === "string") {
			return;
		}

		const cls = node.props?.class;

		if (typeof cls === "string") {
			out.push(...cls.split(" "));
		} else if (Array.isArray(cls)) {
			out.push(...(cls as string[]));
		}
	});
	return out;
}

function hasType(nodes: unknown, type: string, cls?: string): boolean {
	let found = false;
	eachNode(nodes, (node) => {
		if (typeof node === "string") {
			return;
		}

		if (node.type !== type) {
			return;
		}

		if (cls === undefined) {
			found = true;
			return;
		}

		const c = node.props?.class;
		const list = typeof c === "string" ? c.split(" ") : Array.isArray(c) ? c : [];

		if ((list as string[]).includes(cls)) {
			found = true;
		}
	});
	return found;
}

function htmlOf(nodes: unknown, cls: string): string[] {
	const out: string[] = [];
	eachNode(nodes, (node) => {
		if (typeof node === "string") {
			return;
		}

		const c = node.props?.class;
		const list = typeof c === "string" ? c.split(" ") : Array.isArray(c) ? c : [];

		if ((list as string[]).includes(cls) && typeof node.props?.innerHTML === "string") {
			out.push(node.props.innerHTML);
		}
	});
	return out;
}

function textOf(nodes: unknown): string {
	let out = "";
	eachNode(nodes, (node) => {
		if (typeof node === "string") {
			out += node;
		} else if (typeof node.children === "string") {
			out += node.children;
		} else if (Array.isArray(node.children)) {
			out += textOf(node.children);
		} else if (typeof node.props?.innerHTML === "string") {
			// Highlighted code / KaTeX: text lives in markup; strip tags.
			out += node.props.innerHTML.replace(/<[^>]*>/g, "");
		}
	});
	return out;
}

describe("Mercury markdown inline rendering", () => {
	it("bolds **text** with the irc-bold class", () => {
		expect(classes(parse("a **bold** move"))).toContain("irc-bold");
		expect(textOf(parse("a **bold** move"))).toContain("bold");
	});

	it("italicizes *text* and strikes ~~text~~", () => {
		const cls = classes(parse("*it* and ~~gone~~"));
		expect(cls).toContain("irc-italic");
		expect(cls).toContain("irc-strikethrough");
	});

	it("renders `code` as monospace", () => {
		expect(classes(parse("run `npm test` now"))).toContain("irc-monospace");
	});

	it("protects shell punctuation and display-math markers inside inline code", () => {
		for (const command of ['echo "$A-$B" *_file', "echo $$ **literal**", 'printf "\\\\n"']) {
			const nodes = parse(`Run \`${command}\` now; $x^2$ is math.`);
			expect(textOf(nodes)).toContain(command);
			expect(classes(nodes)).toContain("irc-monospace");
			expect(htmlOf(nodes, "md-math")).toHaveLength(1);
			expect(hasType(nodes, "div", "md-math-display")).toBe(false);
		}
	});

	it("protects commands containing literal backticks with a longer delimiter", () => {
		const command = 'echo `date` "$A-$B"';
		const nodes = parse(`Use \`\`${command}\`\` here.`);
		expect(textOf(nodes)).toContain(command);
		expect(classes(nodes)).toContain("irc-monospace");
		expect(htmlOf(nodes, "md-math")).toHaveLength(0);
	});

	it("leaves intra-word asterisks alone", () => {
		expect(classes(parse("2*3*4"))).not.toContain("irc-italic");
	});

	it("keeps links working inside bold text", () => {
		expect(hasType(parse("**see https://example.com/x**"), "a")).toBe(true);
	});
});

describe("Mercury markdown block rendering", () => {
	it("wraps `# ` lines in md-h1", () => {
		expect(hasType(parse("# Title"), "div", "md-h1")).toBe(true);
	});

	it("leaves #channel alone (no space, no heading)", () => {
		expect(hasType(parse("#nixpi4b_gateway hi"), "div", "md-h1")).toBe(false);
	});

	it("wraps `- ` lines in md-ul and `> ` in md-quote", () => {
		expect(hasType(parse("- item"), "div", "md-ul")).toBe(true);
		expect(hasType(parse("> quoted"), "div", "md-quote")).toBe(true);
	});

	it("renders fenced blocks as highlighted pre", () => {
		const nodes = parse("before\n```js\nconst x = 1;\n```\nafter");
		expect(hasType(nodes, "pre", "md-code")).toBe(true);
		expect(textOf(nodes)).toContain("const x = 1;");
		expect(textOf(nodes)).toContain("before");
	});

	it("treats an unclosed fence as code to the end", () => {
		expect(hasType(parse("```py\nx = 1"), "pre", "md-code")).toBe(true);
	});
});

describe("Mercury LaTeX rendering", () => {
	it("renders $…$ inline via KaTeX", () => {
		const html = htmlOf(parse("energy $E=mc^2$ here"), "md-math");
		expect(html.length).toBe(1);
		expect(html[0]).toContain("katex");
	});

	it("renders $$…$$ as a display block", () => {
		const nodes = parse("before\n$$\\frac{a}{b}$$\nafter");
		expect(hasType(nodes, "div", "md-math-display")).toBe(true);
	});

	it("supports explicit TeX delimiters and protects both fence styles", () => {
		expect(htmlOf(parse("Here \\(x^2\\) and \\[x^2\\]"), "md-math")).toHaveLength(2);

		for (const fence of ["~~~", "````"]) {
			const nodes = parse(`${fence}bash\necho $$ **literal** \\(x\\)\n${fence}`);
			expect(hasType(nodes, "pre", "md-code")).toBe(true);
			expect(htmlOf(nodes, "md-math")).toHaveLength(0);
			expect(hasType(nodes, "div", "md-math-display")).toBe(false);
		}
	});

	it("leaves currency ($5 and $10) alone", () => {
		const nodes = parse("costs $5 and $10 total");
		expect(hasType(nodes, "span", "md-math")).toBe(false);
		expect(textOf(nodes)).toContain("$5 and $10");
	});
});

describe("Mercury renderer safety", () => {
	it("escapes raw HTML, never emits script elements", () => {
		const nodes = parse("<script>alert(1)</script>");
		expect(hasType(nodes, "script")).toBe(false);
		expect(textOf(nodes)).toContain("<script>");
	});

	it("escapes HTML inside fenced code", () => {
		const nodes = parse("```html\n<script>alert(1)</script>\n```");
		expect(hasType(nodes, "script")).toBe(false);
	});
});
