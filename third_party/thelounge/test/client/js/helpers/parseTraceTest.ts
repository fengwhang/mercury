// @vitest-environment jsdom
// Mercury: agent execution traces bypass the markdown pipeline (plaintext)
// while the multiline pipeline still applies. Agent replies and human
// messages keep full markdown.
import {expect, describe, it, vi} from "vitest";

vi.mock("../../../../client/js/socket", () => ({default: {}}));

import parse from "../../../../client/js/helpers/parse";
import {isTraceLine} from "../../../../client/js/helpers/markdown";

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

function textOf(nodes: unknown): string {
	let out = "";
	eachNode(nodes, (node) => {
		if (typeof node === "string") {
			out += node;
		} else if (node && typeof node === "object") {
			if (typeof node.children === "string") {
				out += node.children;
			} else if (Array.isArray(node.children)) {
				out += textOf(node.children);
			} else if (typeof node.props?.innerHTML === "string") {
				out += node.props.innerHTML.replace(/<[^>]*>/g, "");
			}
		}
	});
	return out;
}

function hasBr(nodes: unknown): boolean {
	let found = false;
	eachNode(nodes, (node) => {
		if (node && typeof node === "object" && (node as VNode).type === "br") {
			found = true;
		}
	});
	return found;
}

describe("Mercury trace plaintext rendering", () => {
	it("detects the server trace prefixes", () => {
		for (const line of [
			"🔧 read {\"path\": \"/a_b\"}",
			"💭 thinking about *x*",
			"ℹ️ subagent 'n': done",
			"🚀 spawned omp agent 'm'",
			"✅ subagent 'n' finished",
			"🌀 mnemosyne — recalled 4 memories",
			"👁️ Hindsight — recalled 2 memories",
			"🧠 Notes — recalled 2 memories",
		]) {
			expect(isTraceLine(line)).toBe(true);
		}

		expect(isTraceLine("hello *world*")).toBe(false);
		expect(isTraceLine("run `npm test` now")).toBe(false);
		expect(isTraceLine("energy $E=mc^2$ here")).toBe(false);
	});

	it("leaves underscores, asterisks and dollars literal in trace lines", () => {
		const nodes = parse("🔧 read {\"path\": \"/my_dir/my_file_v2\"} cost $5 *fast*");
		const cls = classes(nodes);
		expect(cls).not.toContain("irc-italic");
		expect(cls).not.toContain("irc-bold");
		expect(cls).not.toContain("irc-monospace");
		const text = textOf(nodes);
		expect(text).toContain("/my_dir/my_file_v2");
		expect(text).toContain("$5");
	});

	it("renders thought, lifecycle and memory lines plaintext", () => {
		for (const line of [
			"💭 check snake_case and *stars* $here$",
			"✅ subagent 'worker_1' finished: did_things",
			"🌀 mnemosyne — recalled 4_memories",
		]) {
			const cls = classes(parse(line));
			expect(cls).not.toContain("irc-italic");
			expect(cls).not.toContain("irc-bold");
			expect(textOf(parse(line))).toContain(line.slice(2).split(" ")[0]);
		}
	});

	it("keeps markdown for ordinary user and reply text", () => {
		expect(classes(parse("a **bold** move"))).toContain("irc-bold");
		expect(classes(parse("*it* works"))).toContain("irc-italic");
	});

	it("keeps the multiline pipeline for trace messages", () => {
		const nodes = parse("🔧 first_line\nsecond_line");
		expect(hasBr(nodes)).toBe(true);
		expect(textOf(nodes)).toContain("first_line");
		expect(textOf(nodes)).toContain("second_line");
	});
});
