import { describe, expect, it, spyOn } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { defaultLocalModelInitializer } from "../src/core/embeddings";
import * as runtime from "../src/core/fastembed-runtime";

const MODEL = "fast-multilingual-e5-large";
describe("local-only fastembed initialization", () => {
	it("rejects absent assets before loading the backend or fetching", async () => {
		const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), "mnemopi-offline-"));
		const backend = spyOn(runtime, "loadFastembed").mockRejectedValue(new Error("backend must not load"));
		const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("network must not run"));
		try {
			await expect(defaultLocalModelInitializer({ model: MODEL as never, cacheDir })).rejects.toThrow(/local.*assets/i);
			expect(backend).not.toHaveBeenCalled();
			expect(network).not.toHaveBeenCalled();
		} finally {
			backend.mockRestore(); network.mockRestore();
			await fs.rm(cacheDir, { recursive: true, force: true });
		}
	});
});
