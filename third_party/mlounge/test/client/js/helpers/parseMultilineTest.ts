// @vitest-environment jsdom
// Mercury: a server-reassembled multiline message (one Msg with embedded
// newlines) must render as ONE row with real line breaks.
import {expect, describe, it, vi} from "vitest";

vi.mock("../../../../client/js/socket", () => ({default: {}}));

import parse from "../../../../client/js/helpers/parse";
function eachNode(nodes: unknown, visit: (node: unknown) => void): void {
	const list = Array.isArray(nodes) ? nodes : [nodes];
	for (const node of list) {
		if (Array.isArray(node)) {
			eachNode(node, visit);
		} else {
			visit(node);
		}
	}
}

function hasBr(nodes: unknown): boolean {
	let found = false;
	eachNode(nodes, (node) => {
		if (node && typeof node === "object") {
			const vnode = node as {type?: unknown; children?: unknown};
			if (vnode.type === "br") {
				found = true;
			} else if (vnode.children !== undefined) {
				eachNode(vnode.children, (inner) => {
					if (inner && typeof inner === "object" &&
						(inner as {type?: unknown}).type === "br") {
						found = true;
					}
				});
			}
		}
	});
	return found;
}

function textOf(nodes: unknown): string {
	let out = "";
	eachNode(nodes, (node) => {
		if (typeof node === "string") {
			out += node;
		} else if (node && typeof node === "object") {
			const vnode = node as {children?: unknown};
			if (typeof vnode.children === "string") {
				out += vnode.children;
			}
		}
	});
	return out;
}

describe("Mercury multiline message rendering", () => {
	it("renders embedded newlines as line breaks in one row", () => {
		const nodes = parse("line one\nline two", undefined, undefined);

		expect(textOf(nodes)).toContain("line one");
		expect(textOf(nodes)).toContain("line two");
		expect(hasBr(nodes)).toBe(true);
	});

	it("renders single-line messages unchanged", () => {
		const nodes = parse("just one line", undefined, undefined);

		expect(textOf(nodes)).toContain("just one line");
		expect(hasBr(nodes)).toBe(false);
	});
});
