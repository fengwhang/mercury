import { expect, test } from "bun:test";
import * as path from "node:path";
import { nativeVersionDir } from "../native/loader-state.js";

test("glibc and musl builds cannot reuse each other's version-matched addon", () => {
	const root = path.join("tmp", "native-cache");
	const gnu = nativeVersionDir(root, "18.1.6", "glibc");
	const musl = nativeVersionDir(root, "18.1.6", "musl");
	expect(gnu).toBe(nativeVersionDir(root, "18.1.6"));
	expect(musl).not.toBe(gnu);
	expect(musl).toBe(path.join(gnu, "musl"));
	expect(nativeVersionDir(root, "18.1.7", "musl")).not.toBe(musl);
});
