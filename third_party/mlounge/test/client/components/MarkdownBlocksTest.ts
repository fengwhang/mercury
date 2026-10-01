// @vitest-environment jsdom
import {afterEach, describe, expect, it, vi} from "vitest";
import {mount, type VueWrapper} from "@vue/test-utils";
vi.mock("../../../client/js/socket", () => ({default: {}}));
import MessageBody from "../../../client/components/MessageBody.vue";
import type {ClientMessage} from "../../../client/js/types";

const wrappers: VueWrapper[] = [];

function render(text: string, mercuryKind = "assistant_reply") {
	const wrapper = mount(MessageBody, {
		props: {message: {text, mercuryKind} as ClientMessage},
	});
	wrappers.push(wrapper);
	return wrapper;
}

afterEach(() => {
	for (const wrapper of wrappers.splice(0)) {
		wrapper.unmount();
	}
});

describe("mLounge Markdown blocks", () => {
	it.each(["-", "*", "+"])(
		"groups %s bullets with nested items and continuation lines",
		(marker) => {
			const wrapper = render(
				`${marker} **Parent**\n  continued\n  ${marker} Child\n${marker} Second`
			);
			expect(wrapper.findAll(".message-body > ul > li")).toHaveLength(2);
			expect(wrapper.get("ul > li > ul > li").text()).toBe("Child");
			expect(wrapper.get(".irc-bold").text()).toBe("Parent");
			expect(wrapper.get("ul > li").text()).toContain("continued");
			expect(wrapper.get("ul > li br").exists()).toBe(true);
		}
	);

	it.each([".", ")"])(
		"renders ordered %s lists with a non-default start and nested bullets",
		(marker) => {
			const wrapper = render(`7${marker} First\n   - Detail\n8${marker} Second`);
			expect(wrapper.get("ol").attributes("start")).toBe("7");
			expect(wrapper.findAll("ol > li")).toHaveLength(2);
			expect(wrapper.get("ol > li > ul > li").text()).toBe("Detail");
		}
	);

	it("keeps paragraphs, fences and math inside their list item", () => {
		const command = 'echo "$A-$B" *.txt **literal**';
		const wrapper = render(
			`1. Run this:\n\n   Second paragraph.\n\n   \`\`\`bash\n   ${command}\n   \`\`\`\n\n   $$x^2$$\n\n2. Done`
		);
		expect(wrapper.findAll("ol > li")).toHaveLength(2);
		expect(wrapper.findAll("ol > li:first-child > p")).toHaveLength(2);
		expect(wrapper.get("ol > li:first-child > pre code").element.textContent).toBe(command);
		expect(wrapper.get("ol > li:first-child > .md-math-display").exists()).toBe(true);
		expect(wrapper.findAll(".irc-bold")).toHaveLength(0);
	});

	it("renders aligned tables with code, math, links and escaped pipes", () => {
		const wrapper = render(
			"| **Name** | Command | Result |\n| :--- | :---: | ---: |\n" +
				'| Mercury | `echo "$A-$B" *_file` | $x^2$ |\n' +
				"| https://example.com | `a\\|b` | left\\|right |"
		);
		expect(wrapper.findAll(".md-table-scroll > table > thead > tr > th")).toHaveLength(3);
		expect(wrapper.findAll("table > tbody > tr")).toHaveLength(2);
		expect(wrapper.findAll("td")).toHaveLength(6);
		expect(wrapper.get("th .irc-bold").text()).toBe("Name");
		expect(
			wrapper.findAll("th").map((cell) => (cell.element as HTMLElement).style.textAlign)
		).toEqual(["left", "center", "right"]);
		expect(wrapper.get("td code").element.textContent).toBe('echo "$A-$B" *_file');
		expect(wrapper.get("td .md-math").exists()).toBe(true);
		expect(wrapper.get("td a").attributes("href")).toBe("https://example.com");
		expect(
			wrapper
				.findAll("tbody tr")[1]
				.findAll("td")
				.map((cell) => cell.text())
		).toEqual(["https://example.com", "a|b", "left|right"]);
	});

	it("accepts tables without outer pipes and pads missing cells", () => {
		const wrapper = render("Name | Value\n--- | ---\nFirst | 1\nSecond");
		expect(wrapper.findAll("tbody tr")).toHaveLength(2);
		expect(
			wrapper
				.findAll("tbody tr")[1]
				.findAll("td")
				.map((cell) => cell.text())
		).toEqual(["Second", ""]);
	});

	it("renders nested quotes and multiline display math without stray line breaks", () => {
		const wrapper = render("> Heading\n>\n> - item\n>   - detail\n\n$$\nx^2 +\ny^2\n$$\nAfter");
		expect(wrapper.get("blockquote ul ul li").text()).toBe("detail");
		expect(wrapper.findAll(".md-math-display")).toHaveLength(1);
		expect(wrapper.find(".message-body > br").exists()).toBe(false);
		expect(wrapper.text()).toContain("After");
	});

	it("keeps raw HTML inert inside table cells", () => {
		const wrapper = render(
			"| HTML | Code |\n| --- | --- |\n| <img src=x onerror=alert(1)> | `<script>bad()</script>` |"
		);
		expect(wrapper.find("img, script").exists()).toBe(false);
		expect(wrapper.findAll("td").map((cell) => cell.text())).toEqual([
			"<img src=x onerror=alert(1)>",
			"<script>bad()</script>",
		]);
	});

	it.each(["tool_input", "tool_output", "thinking", "status"])(
		"keeps %s lists and tables as plaintext",
		(kind) => {
			const source = "1. literal\n- literal\n| A | B |\n| --- | --- |\n| $X$ | *_file |";
			const wrapper = render(source, kind);
			expect(wrapper.get(".message-plaintext").element.textContent).toBe(source);
			expect(wrapper.find("ol, ul, table, .md-math").exists()).toBe(false);
		}
	);

	it("toggles structured Markdown to its exact raw source and back", async () => {
		const source = "3. First\n4. Second\n\n| A | B |\n| --- | --- |\n| one | two |";
		const wrapper = render(source);
		expect(wrapper.find("ol").exists()).toBe(true);
		expect(wrapper.find("table").exists()).toBe(true);
		await wrapper.get(".message-tools button").trigger("click");
		expect(wrapper.get(".message-raw").element.textContent).toBe(source);
		expect(wrapper.find("ol, table").exists()).toBe(false);
		await wrapper.get(".message-tools button").trigger("click");
		expect(wrapper.get("ol").attributes("start")).toBe("3");
		expect(wrapper.find("table").exists()).toBe(true);
	});
});
