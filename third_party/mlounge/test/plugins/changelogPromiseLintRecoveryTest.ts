import {describe, expect, it, vi} from "vitest";
import changelog from "../../server/plugins/changelog";
import pkg from "../../package.json";

describe("Mercury offline changelog Promise contract", () => {
	it("resolves installed metadata through then and await without fetching or timers", async () => {
		const fetchSpy = vi.spyOn(globalThis, "fetch");
		const timerSpy = vi.spyOn(globalThis, "setInterval");

		try {
			changelog.checkForUpdates({} as never);
			const result = changelog.fetch();
			let called = false;
			const thenResult = result.then((data) => {
				called = true;
				return data;
			});

			expect(called).toBe(false);
			const data = await thenResult;

			expect(await result).toBe(data);
			expect(data.current.version).toBe(`v${pkg.version}`);
			expect(data.current.url).toBe("");
			expect(data.latest).toBeUndefined();
			expect(data.expiresAt).toBe(-1);
			expect(changelog.isUpdateAvailable).toBe(false);
			expect(fetchSpy).not.toHaveBeenCalled();
			expect(timerSpy).not.toHaveBeenCalled();
		} finally {
			fetchSpy.mockRestore();
			timerSpy.mockRestore();
		}
	});

	it("preserves the caller Promise.all rejection path", async () => {
		const failure = new Error("package metadata unavailable");
		const caller = Promise.all([changelog.fetch(), Promise.reject(failure)]);

		await expect(caller).rejects.toBe(failure);
	});
});
