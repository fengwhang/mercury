import {expect, test, vi} from "vitest";
const remote = vi.hoisted(() => vi.fn(() => Promise.reject(new Error("fixture remote boundary"))));
vi.mock("got", () => ({default: remote}));
import changelog from "../../server/plugins/changelog";

test("changelog serves local version without querying vendor releases", async () => {
	const data = await changelog.fetch();
	expect(data.current.version).toBe("v0.4.5");
	expect(data.latest).toBeUndefined();
	expect(remote).not.toHaveBeenCalled();
});
