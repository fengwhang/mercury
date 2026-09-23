import { describe, expect, it } from "bun:test";
import { localSlashResponse } from "@oh-my-pi/pi-coding-agent/modes/rpc/rpc-local-slash";

describe("localSlashResponse", () => {
	it("returns null for plain chat and unknown commands", () => {
		expect(localSlashResponse("hello there")).toBeNull();
		expect(localSlashResponse("/nope123")).toBeNull();
		expect(localSlashResponse("")).toBeNull();
		expect(localSlashResponse("!help")).toBeNull();
	});

	it("returns null for commands with a headless handle", () => {
		expect(localSlashResponse("/compact")).toBeNull();
		expect(localSlashResponse("/model opus")).toBeNull();
	});

	it("answers /help with the command list", () => {
		const text = localSlashResponse("/help");
		expect(text).not.toBeNull();
		expect(text).toContain("Available commands:");
		expect(text).toContain("/compact");
		expect(text).toContain("/model");
	});

	it("answers /help <topic>", () => {
		const text = localSlashResponse("/help compact");
		expect(text).not.toBeNull();
		expect(text).toContain("/compact");
		expect(localSlashResponse("/help nope123")).toContain("No such command");
	});

	it("refuses interactive-only commands honestly", () => {
		const text = localSlashResponse("/clear");
		expect(text).not.toBeNull();
		expect(text).toContain("can't run over this channel");
	});
});
