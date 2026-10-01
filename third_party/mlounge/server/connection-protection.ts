import childProcess from "child_process";
import net from "net";
import os from "os";

type ConnectionSocket = {
	remoteAddress?: string;
	localAddress?: string;
	encrypted?: boolean;
	authorized?: boolean;
};

let selfAddresses = new Set<string>();
let peerAddresses = new Set<string>();
let nextRefresh = 0;
let refreshing: Promise<void> | undefined;

function address(value: string | undefined): string {
	if (!value) {
		return "";
	}

	if (net.isIPv4(value)) {
		return value;
	}

	const unscoped = value.split("%")[0];

	if (!net.isIPv6(unscoped)) {
		return "";
	}

	const normalized = new URL(`http://[${unscoped}]`).hostname.slice(1, -1);
	const mapped = /^::ffff:([\da-f]+):([\da-f]+)$/.exec(normalized);

	if (!mapped) {
		return normalized;
	}

	const high = parseInt(mapped[1], 16);
	const low = parseInt(mapped[2], 16);
	return `${high >> 8}.${high & 255}.${low >> 8}.${low & 255}`;
}

function localAddresses(): Set<string> {
	try {
		return new Set(
			Object.values(os.networkInterfaces()).flatMap((entries) =>
				(entries || []).map((entry) => address(entry.address))
			)
		);
	} catch {
		return new Set();
	}
}

function isLocal(socket: ConnectionSocket): boolean {
	const remote = address(socket.remoteAddress);
	return (
		remote === "::1" ||
		remote.startsWith("127.") ||
		(remote !== "" && localAddresses().has(remote))
	);
}

function isTailnetAddress(value: string | undefined): boolean {
	const normalized = address(value);

	if (normalized.startsWith("fd7a:115c:a1e0:")) {
		return true;
	}

	const [first, second] = normalized.split(".").map(Number);
	return first === 100 && second >= 64 && second <= 127;
}

// The address range alone does not prove Tailscale: carriers and other VPNs
// also use CGNAT. Verify the resolved endpoints against the local daemon.
export async function refreshConnectionProtection(
	socket: ConnectionSocket,
	proxied: boolean
): Promise<void> {
	if (
		(socket.encrypted && socket.authorized === true) ||
		proxied ||
		isLocal(socket) ||
		!isTailnetAddress(socket.remoteAddress) ||
		!isTailnetAddress(socket.localAddress)
	) {
		return;
	}

	if (refreshing) {
		return refreshing;
	}

	if (Date.now() < nextRefresh) {
		return;
	}

	refreshing = (async () => {
		try {
			const output = await new Promise<string>((resolve, reject) => {
				childProcess.execFile(
					"tailscale",
					["status", "--json"],
					{timeout: 2000, maxBuffer: 4 * 1024 * 1024},
					(error, stdout) => {
						if (error) {
							reject(error);
						} else {
							resolve(stdout);
						}
					}
				);
			});
			const data: unknown = JSON.parse(output);

			if (!data || typeof data !== "object") {
				throw new Error("Invalid Tailscale status");
			}

			const status = data as {
				BackendState?: string;
				Self?: {TailscaleIPs?: unknown};
				Peer?: Record<string, {TailscaleIPs?: unknown}>;
			};

			if (status.BackendState !== "Running") {
				throw new Error("Tailscale is not running");
			}

			const ips = (value: unknown): string[] =>
				Array.isArray(value)
					? value
							.filter((ip): ip is string => typeof ip === "string")
							.map(address)
							.filter(Boolean)
					: [];
			selfAddresses = new Set(ips(status.Self?.TailscaleIPs));
			peerAddresses = new Set([
				...selfAddresses,
				...Object.values(status.Peer || {}).flatMap((peer) => ips(peer?.TailscaleIPs)),
			]);
		} catch {
			selfAddresses.clear();
			peerAddresses.clear();
		} finally {
			nextRefresh = Date.now() + 30_000;
			refreshing = undefined;
		}
	})();
	return refreshing;
}

export function connectionProtection(
	socket: ConnectionSocket,
	proxied: boolean
): {secure: boolean; warning?: string} {
	if (socket.encrypted && socket.authorized === true) {
		return {secure: true};
	}

	if (proxied) {
		return {
			secure: false,
			warning: socket.encrypted
				? "TLS certificate validation failed"
				: "Unencrypted connection through a proxy",
		};
	}

	if (isLocal(socket)) {
		return {secure: true};
	}

	const local = address(socket.localAddress);
	const remote = address(socket.remoteAddress);

	if (selfAddresses.has(local) && peerAddresses.has(remote) && localAddresses().has(local)) {
		return {secure: true};
	}

	return {
		secure: false,
		warning: socket.encrypted
			? "TLS certificate validation failed"
			: "Connection is not protected by TLS, localhost, or verified Tailscale",
	};
}
