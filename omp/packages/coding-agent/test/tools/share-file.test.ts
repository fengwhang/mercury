import * as fs from "node:fs";
import * as fsPromises from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, spyOn } from "bun:test";
import { ShareFileTool } from "@oh-my-pi/pi-coding-agent/tools/share-file";

let home: string;
let oldMercuryHome: string | undefined;

beforeEach(() => {
	home = fs.mkdtempSync(join(tmpdir(), "share-file-test-"));
	oldMercuryHome = process.env.MERCURY_HOME;
	fs.writeFileSync(join(home, "room.log"), "private fake room: no publication\n");
	process.env.MERCURY_HOME = home;
});

afterEach(() => {
	if (oldMercuryHome === undefined) delete process.env.MERCURY_HOME;
	else process.env.MERCURY_HOME = oldMercuryHome;
	fs.rmSync(home, { recursive: true, force: true });
});

async function expectUnavailableWithoutSideEffects(params: { path: string; caption?: string }) {
	const snapshot = () =>
		fs.readdirSync(home, { recursive: true }).map(entry => {
			const relative = String(entry);
			const file = join(home, relative);
			return [relative, fs.statSync(file).isFile() ? fs.readFileSync(file).toString("base64") : null];
		});
	const before = snapshot();
	const spies = [
		spyOn(Bun, "file"),
		spyOn(Bun, "write"),
		spyOn(globalThis, "fetch"),
		spyOn(fs, "readFileSync"),
		spyOn(fs, "writeFileSync"),
		spyOn(fs, "copyFileSync"),
		spyOn(fs, "mkdirSync"),
		spyOn(fs, "statSync"),
		spyOn(fsPromises, "readFile"),
		spyOn(fsPromises, "writeFile"),
		spyOn(fsPromises, "copyFile"),
		spyOn(fsPromises, "mkdir"),
		spyOn(fs, "lstatSync"),
		spyOn(fs, "accessSync"),
		spyOn(fs, "openSync"),
		spyOn(fs, "existsSync"),
		spyOn(fs, "readdirSync"),
		spyOn(fs, "renameSync"),
		spyOn(fs, "unlinkSync"),
		spyOn(fsPromises, "lstat"),
		spyOn(fsPromises, "access"),
		spyOn(fsPromises, "open"),
		spyOn(fsPromises, "readdir"),
		spyOn(fsPromises, "rename"),
		spyOn(fsPromises, "unlink"),
		spyOn(fsPromises, "rm"),
		spyOn(fsPromises, "stat"),
	];
	try {
		const tool = new ShareFileTool();
		const result = await tool.execute("call-1", params);
		expect(tool.label).toContain("unavailable");
		expect(result.isError).toBe(true);
		expect(result.content).toEqual([{ type: "text", text: tool.description }]);
		expect(tool.description).toContain("publication is excluded");
		expect(result.details).toEqual({ url: "", filename: "" });
		for (const spy of spies) expect(spy).not.toHaveBeenCalled();
	} finally {
		for (const spy of spies) spy.mockRestore();
	}
	expect(snapshot()).toEqual(before);
}

describe("share_file", () => {
	it("reports unavailable without staging a file or returning a room-postable URL", async () => {
		const src = join(home, "share-src.pdf");
		fs.writeFileSync(src, "%PDF-1.4 data");
		await expectUnavailableWithoutSideEffects({ path: src, caption: "read this" });
		expect(fs.readFileSync(src, "utf8")).toBe("%PDF-1.4 data");
	});

	it("refuses missing files, directories, and secrets without inspecting or publishing them", async () => {
		fs.mkdirSync(join(home, "sub"));
		fs.writeFileSync(join(home, "id_rsa.key"), "private fake secret");
		for (const path of [join(home, "nope.txt"), join(home, "sub"), join(home, "id_rsa.key"), "/etc/hostname"]) {
			await expectUnavailableWithoutSideEffects({ path });
		}
		expect(fs.readFileSync(join(home, "id_rsa.key"), "utf8")).toBe("private fake secret");
	});
});
