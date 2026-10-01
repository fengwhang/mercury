import {expect, describe, it, vi} from "vitest";
import {Client as MircClient} from "irc-framework";
import type Client from "../../../server/client";
import Chan from "../../../server/models/chan";
import Msg from "../../../server/models/msg";
import Network, {type NetworkWithIrcFramework} from "../../../server/models/network";
import {ChanState} from "../../../shared/types/chan";
import {MessageType} from "../../../shared/types/msg";
import join from "../../../server/plugins/irc-events/join";
import part from "../../../server/plugins/irc-events/part";
import quit from "../../../server/plugins/irc-events/quit";
import topic from "../../../server/plugins/irc-events/topic";
import invite from "../../../server/plugins/irc-events/invite";

function harness(mercury = true) {
	const mirc = new MircClient();
	mirc.user.nick = "owner";
	const room = new Chan({name: "#room", state: ChanState.PARTED});
	const network = new Network({channels: [room], irc: mirc});
	const pushed: Msg[] = [];

	for (const chan of network.channels) {
		vi.spyOn(chan, "pushMessage").mockImplementation((_client, msg) => {
			pushed.push(msg);
		});
	}

	const client = {
		emit: vi.fn(),
		part: vi.fn(),
		save: vi.fn(),
		createChannel: vi.fn(({name}) => {
			const chan = new Chan({name});
			vi.spyOn(chan, "pushMessage").mockImplementation((_client, msg) => {
				pushed.push(msg);
			});
			vi.spyOn(chan, "loadMessages").mockImplementation(() => undefined);
			return chan;
		}),
	};

	for (const handler of [join, part, quit, topic, invite]) {
		handler.call(
			client as unknown as Client,
			mirc as NetworkWithIrcFramework["irc"],
			network as NetworkWithIrcFramework
		);
	}

	// Use the real parser for the marker as well as the lifecycle messages.
	const read = (line: string) => {
		mirc.connection.addReadBuffer(line);
	};

	read(`:vm 005 owner CHANTYPES=#${mercury ? " MERCURY=1" : ""} :supported`);
	return {mirc, network, room, client, pushed, read};
}

describe("MIRC room restoration", () => {
	it("keeps membership and topics up to date without storing reconnect chatter", async () => {
		const {room, client, pushed, read} = harness();

		for (const line of [
			":owner!owner@vm JOIN #room",
			":vm 332 owner #room :Agent work",
			":vm 333 owner #room vm_gateway 1000",
			":vm_gateway!gateway@vm INVITE owner :#room",
			":probe!probe@vm JOIN #room",
			":probe!probe@vm PART #room :health check complete",
			":helper!helper@vm JOIN #room",
			":helper!helper@vm QUIT :restart",
		]) {
			read(line);
		}

		await vi.waitFor(() => expect(room.topic).toBe("Agent work"));
		expect(room.state).toBe(ChanState.JOINED);
		expect(room.findUser("owner")).toBeDefined();
		expect(room.findUser("probe")).toBeUndefined();
		expect(room.findUser("helper")).toBeUndefined();
		expect(client.emit).toHaveBeenCalledWith("channel:state", {
			chan: room.id,
			state: ChanState.JOINED,
		});
		expect(pushed).toHaveLength(0);
	});

	it("still creates new auto-joined rooms and surfaces genuine topic edits", async () => {
		const {network, client, pushed, read} = harness();
		read(":owner!owner@vm JOIN #new-room");
		read(":vm_gateway!gateway@vm TOPIC #new-room :A new task");
		await vi.waitFor(() => expect(pushed).toHaveLength(1));
		expect(network.getChannel("#new-room")?.topic).toBe("A new task");
		expect(client.emit).toHaveBeenCalledWith(
			"join",
			expect.objectContaining({shouldOpen: false})
		);
		expect(pushed[0].type).toBe(MessageType.TOPIC);
		expect(pushed[0].text).toBe("A new task");
	});

	it("retains ordinary membership, topic, and invite notices on other networks", async () => {
		const {pushed, read} = harness(false);
		read(":owner!owner@vm JOIN #room");
		read(":vm 332 owner #room :Agent work");
		read(":vm_gateway!gateway@vm INVITE owner :#room");
		await vi.waitFor(() => expect(pushed).toHaveLength(3));
		expect(pushed.map((msg) => msg.type)).toEqual([
			MessageType.JOIN,
			MessageType.TOPIC,
			MessageType.INVITE,
		]);
	});
});
