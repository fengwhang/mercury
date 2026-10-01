// @vitest-environment jsdom
import {describe, expect, it, vi} from "vitest";
import {mount} from "@vue/test-utils";
vi.mock("../../../client/js/socket", () => ({default: {}}));
vi.mock("../../../client/js/helpers/collapseNetwork", () => ({default: vi.fn()}));
vi.mock("../../../client/components/ChannelWrapper.vue", () => ({
	default: {template: "<div><slot /></div>"},
}));
import NetworkLobby from "../../../client/components/NetworkLobby.vue";
import type {ClientNetwork} from "../../../client/js/types";
import type {SharedNetworkStatus} from "../../../shared/types/network";

function render(status: SharedNetworkStatus) {
	return mount(NetworkLobby, {
		props: {
			network: {
				uuid: "mirc",
				status,
				isCollapsed: false,
				channels: [{id: 1, name: "Mercury", unread: 0}],
			} as ClientNetwork,
		},
		global: {stubs: {ChannelWrapper: {template: "<div><slot /></div>"}}},
	});
}

describe("mLounge network warnings", () => {
	it("does not show a warning for a protected connection", () => {
		const wrapper = render({connected: true, secure: true});
		expect(wrapper.find(".not-secure-icon").exists()).toBe(false);
		expect(wrapper.find(".not-connected-icon").exists()).toBe(false);
		wrapper.unmount();
	});

	it.each([
		"TLS certificate validation failed",
		"Unencrypted connection through a proxy",
		"Connection is not protected by TLS, localhost, or verified Tailscale",
	])("shows and explains the actual protection problem: %s", (warning) => {
		const wrapper = render({connected: true, secure: false, warning});
		expect(wrapper.find(".not-secure-icon").exists()).toBe(true);
		expect(wrapper.get(".not-secure-tooltip").attributes("aria-label")).toBe(warning);
		wrapper.unmount();
	});

	it("shows the disconnected indicator separately", () => {
		const wrapper = render({connected: false, secure: false});
		expect(wrapper.find(".not-secure-icon").exists()).toBe(false);
		expect(wrapper.get(".not-connected-tooltip").attributes("aria-label")).toBe("Disconnected");
		wrapper.unmount();
	});
});
