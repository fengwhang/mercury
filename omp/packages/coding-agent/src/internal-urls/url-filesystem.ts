/**
 * Host-filesystem view for semantic find (`omp find`).
 *
 * Mercury-scoped port of upstream `internal-urls/url-filesystem.ts`: the
 * upstream file bridges host paths and every internal-URL scheme through the
 * v18.6.0 router/handlers (`SchemeSpec`, `locate`/`enumerate`, shell
 * filesystems for native grep/glob), a subsystem that diverged wholesale from
 * this tree's internal-urls architecture. `find` only needs three operations
 * over the searched root — `stat`, `readPrefix` — so this module implements
 * exactly those for host paths with the same exported names and error shape,
 * and refuses `scheme://` roots with a usage error instead of silently
 * misresolving them.
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { ToolTier } from "@oh-my-pi/pi-agent-core";

const URL_PATH_RE = /^([a-z][a-z0-9+.-]*):\/\/(.*)$/is;

/** Whether `input` is spelled `scheme://…`. */
export function isUrlPath(input: string): boolean {
	return URL_PATH_RE.test(input);
}

/** A filesystem failure carrying the errno name the shell reports. */
export class UrlFsError extends Error {
	override name = "UrlFsError";

	constructor(
		readonly code: string,
		message: string,
	) {
		super(message);
	}
}

export interface UrlFileStat {
	type: "directory" | "file" | "other";
	/** Bytes of a file; 0 for directories. */
	size: number;
	/** Host modification time; 0 for rendered resources, which have none. */
	mtimeMs: number;
}

export interface InternalUrlFilesystemOptions {
	/** Calling session's cwd. */
	context: { cwd: string };
	/** Approval tier the command ran under; reserved for scheme gating. */
	tier: ToolTier;
}

/**
 * Host-path filesystem for `find`: `stat` + `readPrefix` over the local
 * filesystem. Internal-URL roots are rejected — wire them through a
 * scheme-aware filesystem if URL-scoped search is ever needed.
 */
export class InternalUrlFilesystem {
	readonly #cwd: string;

	constructor(options: InternalUrlFilesystemOptions) {
		this.#cwd = options.context.cwd;
	}

	async stat(target: string): Promise<UrlFileStat> {
		if (isUrlPath(target)) {
			throw new UrlFsError("EINVAL", `find searches host paths, not internal URLs: ${target}`);
		}
		const resolved = path.resolve(this.#cwd, target);
		let st;
		try {
			st = await fs.stat(resolved);
		} catch {
			throw new UrlFsError("ENOENT", `Path not found: ${target}`);
		}
		if (st.isDirectory()) return { type: "directory", size: 0, mtimeMs: st.mtimeMs };
		if (st.isFile()) return { type: "file", size: st.size, mtimeMs: st.mtimeMs };
		return { type: "other", size: 0, mtimeMs: st.mtimeMs };
	}

	async readPrefix(target: string, maxBytes: number): Promise<Uint8Array> {
		if (isUrlPath(target)) {
			throw new UrlFsError("EINVAL", `find reads host paths, not internal URLs: ${target}`);
		}
		const handle = await fs.open(path.resolve(this.#cwd, target), "r");
		try {
			const buf = Buffer.alloc(Math.max(0, maxBytes));
			const { bytesRead } = await handle.read(buf, 0, buf.length, 0);
			return buf.subarray(0, bytesRead);
		} finally {
			await handle.close();
		}
	}
}
