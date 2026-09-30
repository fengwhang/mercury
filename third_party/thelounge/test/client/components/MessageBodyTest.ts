// @vitest-environment jsdom
import {describe, expect, it, vi, afterEach} from "vitest";
import {mount, flushPromises} from "@vue/test-utils";
vi.mock("../../../client/js/socket", () => ({default: {}}));
import MessageBody from "../../../client/components/MessageBody.vue";
import type {ClientMessage} from "../../../client/js/types";
import copyText from "../../../client/js/helpers/copyText";

afterEach(() => vi.restoreAllMocks());

describe("Lounge message source and copy controls", () => {
	it("copies literal code without fences, preserving shell symbols and whitespace", async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal("navigator", {clipboard: {writeText}});
		const command = '  printf "%s\\n" "$A-$B" *.txt  \n';
		const text = `**Run this:**\n\`\`\`bash\n${command}\n\`\`\`\nThen $x^2$.`;
		const wrapper = mount(MessageBody, {
			props: {message: {text, mercuryKind: "assistant_reply"} as ClientMessage},
		});
		expect(wrapper.get(".md-code code").text()).toBe(command.trim());
		expect(wrapper.get(".md-code code").element.textContent).toBe(command);
		expect(wrapper.find(".md-math").exists()).toBe(true);
		expect(wrapper.findAll(".message-tools button")).toHaveLength(1);
		expect(wrapper.get(".message-tools button").text()).toBe("Raw");
		await wrapper.get('button[aria-label="Copy code"]').trigger("click");
		await flushPromises();
		expect(writeText).toHaveBeenLastCalledWith(command);
		await wrapper.get(".message-tools button").trigger("click");
		expect(wrapper.get(".message-raw").element.textContent).toBe(text);
		expect(wrapper.find(".md-math").exists()).toBe(false);
		expect(wrapper.get(".message-tools button").attributes("aria-pressed")).toBe("true");
		expect(wrapper.get(".message-tools button").text()).toBe("Raw");
		expect(writeText).toHaveBeenCalledTimes(1);
		await wrapper.get(".message-tools button").trigger("click");
		expect(wrapper.find(".md-math").exists()).toBe(true);
		expect(wrapper.get(".message-tools button").attributes("aria-pressed")).toBe("false");
		wrapper.unmount();
		vi.unstubAllGlobals();
	});

	it("preserves plain trace content even when it looks like Markdown, TeX or HTML", () => {
		const text = "  **literal** $A-$B\n<script>alert(1)</script>\n\t*.txt  ";
		const wrapper = mount(MessageBody, {
			props: {message: {text, mercuryKind: "tool_output"} as ClientMessage},
		});
		expect(wrapper.get(".message-plaintext").element.textContent).toBe(text);
		expect(wrapper.find("script").exists()).toBe(false);
		expect(wrapper.find(".md-math").exists()).toBe(false);
		wrapper.unmount();
	});

	it("copies the same source on HTTP when Clipboard API is unavailable or denied", async () => {
		const text = 'echo "$X" *.txt\n  ';

		for (const clipboard of [
			undefined,
			{writeText: vi.fn().mockRejectedValue(new Error("Denied"))},
		]) {
			vi.stubGlobal("navigator", {clipboard});
			const exec = vi.fn(() => {
				expect((document.activeElement as HTMLTextAreaElement).value).toBe(text);
				return true;
			});
			Object.defineProperty(document, "execCommand", {configurable: true, value: exec});
			await copyText(text);
			expect(exec).toHaveBeenCalledWith("copy");
			expect(document.querySelector("textarea")).toBeNull();
		}

		vi.unstubAllGlobals();
	});
});
