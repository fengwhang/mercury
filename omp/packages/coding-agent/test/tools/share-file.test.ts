import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { ShareFileTool } from "@oh-my-pi/pi-coding-agent/tools/share-file";

let home: string;
let oldMercuryHome: string | undefined;

beforeEach(() => {
	home = mkdtempSync(join(tmpdir(), "share-file-test-"));
	oldMercuryHome = process.env.MERCURY_HOME;
	process.env.MERCURY_HOME = home;
});

afterEach(() => {
	if (oldMercuryHome === undefined) delete process.env.MERCURY_HOME;
	else process.env.MERCURY_HOME = oldMercuryHome;
	rmSync(home, { recursive: true, force: true });
});

function run(params: { path: string; caption?: string }) {
	const tool = new ShareFileTool();
	return tool.execute("call-1", params as never);
}

describe("share_file", () => {
	it("stages a file and returns a room-postable URL", async () => {
		const src = join(tmpdir(), `share-src-${Date.now()}.pdf`);
		writeFileSync(src, "%PDF-1.4 data");
		try {
			const result = await run({ path: src, caption: "read this" });
			const text = result.content.map(part => (part as { text?: string }).text ?? "").join("\n");
			expect(text).toContain("/uploads/");
			expect(text).toContain(".pdf");
			const details = result.details ?? { url: "", filename: "" };
			expect(details.filename).toMatch(/^share-src-.*\.pdf$/);
			const token = (details.url.split("/uploads/")[1] ?? "").split("/")[0];
			expect(token).toMatch(/^[0-9a-f]{16}$/);
		} finally {
			rmSync(src, { force: true });
		}
	});

	it("refuses missing files, directories, and secrets", async () => {
		const missing = await run({ path: join(home, "nope.txt") });
		expect(JSON.stringify(missing)).toContain("file not found");
		mkdirSync(join(home, "sub"));
		const dir = await run({ path: join(home, "sub") });
		expect(JSON.stringify(dir)).toContain("not a regular file");
		writeFileSync(join(home, "id_rsa.key"), "x");
		const key = await run({ path: join(home, "id_rsa.key") });
		expect(JSON.stringify(key)).toContain("refusing");
		const etc = await run({ path: "/etc/hostname" });
		expect(JSON.stringify(etc)).toContain("refusing");
	});
});
