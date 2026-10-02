import { expect, test } from "bun:test";
import { resolveCrossBuild } from "../scripts/build-binary";

test("Linux release targets select official runtimes for each libc and CPU", () => {
	for (const arch of ["x64", "arm64"]) {
		const gnu = resolveCrossBuild(`linux-${arch}`);
		const musl = resolveCrossBuild(`linux-musl-${arch}`);
		expect(gnu?.arch).toBe(arch);
		expect(musl?.arch).toBe(arch);
		expect(gnu?.platform).toBe("linux");
		expect(musl?.libc).toBe("musl");
		expect(gnu?.target).not.toContain("musl");
		expect(musl?.target).toContain("musl");
	}
	expect(resolveCrossBuild("linux-x64")?.target).toContain("baseline");
	expect(() => resolveCrossBuild("linux-riscv64")).toThrow("Unsupported CROSS_TARGET");
});
