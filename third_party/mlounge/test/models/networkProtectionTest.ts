import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import childProcess from "child_process";
import net from "net";
import os from "os";
import {Client as MircClient} from "irc-framework";
import Network from "../../server/models/network";
import {refreshConnectionProtection} from "../../server/connection-protection";

let clock = 0;
const self = "100.100.1.1";
const peer = "100.100.1.2";
const self6 = "fd7a:115c:a1e0::1";
const peer6 = "fd7a:115c:a1e0::2";

beforeEach(() => {
	clock += 60_000;
	vi.spyOn(Date, "now").mockReturnValue(clock);
	const entry = (address: string): os.NetworkInterfaceInfo => ({
		address,
		netmask: "",
		family: net.isIPv6(address) ? "IPv6" : "IPv4",
		mac: "",
		internal: false,
		cidr: null,
	});
	vi.spyOn(os, "networkInterfaces").mockReturnValue({
		tailscale0: [entry(self), entry(self6)],
		eth0: [entry("192.168.1.10")],
	});
});

afterEach(() => vi.restoreAllMocks());

function tailscale(running = true) {
	return vi
		.spyOn(childProcess, "execFile")
		.mockImplementation((_file, _args, _options, callback) => {
			callback?.(
				null,
				JSON.stringify({
					BackendState: running ? "Running" : "Stopped",
					Self: {TailscaleIPs: [self, self6]},
					Peer: {remote: {TailscaleIPs: [peer, peer6]}},
				}),
				""
			);
			return new childProcess.ChildProcess();
		});
}

function network(
	remoteAddress: string,
	localAddress = self,
	encrypted = false,
	authorized = false
) {
	const socket = {remoteAddress, localAddress, encrypted, authorized};
	const client = new MircClient();
	const transport = {socket, isConnected: () => true};
	Object.assign(client.connection, {transport});
	const result = new Network({irc: client});
	return {socket, transport, result};
}

describe("Network protection indicator", () => {
	it.each([
		"127.0.0.1",
		"127.2.3.4",
		"::1",
		"::ffff:127.0.0.1",
		"::ffff:7f00:1",
		self,
		self6,
		"192.168.1.10",
	])("treats a direct connection to this machine (%s) as local", (remote) => {
		const {result} = network(remote);
		expect(result.getNetworkStatus()).toEqual({connected: true, secure: true});
	});

	it.each([
		[peer, self],
		[peer6, self6],
		["::ffff:100.100.1.2", "::ffff:100.100.1.1"],
		["fd7a:115c:a1e0:0:0:0:0:2", "fd7a:115c:a1e0:0:0:0:0:1"],
	])("recognizes resolved Tailscale peers %s -> %s", async (remote, local) => {
		const execute = tailscale();
		const {result} = network(remote, local);
		await result.refreshConnectionProtection();
		expect(execute).toHaveBeenCalledWith(
			"tailscale",
			["status", "--json"],
			expect.any(Object),
			expect.any(Function)
		);
		expect(result.getNetworkStatus()).toEqual({connected: true, secure: true});
	});

	it("does not trust CGNAT ranges, a MIRC server marker, or a private LAN by themselves", async () => {
		tailscale();

		for (const [remote, local] of [
			["100.100.1.3", self],
			[peer, "192.168.1.10"],
			["192.168.1.20", "192.168.1.10"],
			["203.0.113.1", self],
		]) {
			const {result} = network(remote, local);
			await result.refreshConnectionProtection();
			expect(result.getNetworkStatus().secure).toBe(false);
			expect(result.getNetworkStatus().warning).toContain("not protected");
		}
	});

	it("retains certificate warnings even on localhost and verified tailnet peers", async () => {
		tailscale();
		await refreshConnectionProtection(network(peer).socket, false);

		for (const remote of ["127.0.0.1", self, peer]) {
			const {result} = network(remote, self, true, false);
			expect(result.getNetworkStatus().warning).toBe("TLS certificate validation failed");
			expect(result.getNetworkStatus().secure).toBe(false);
		}

		expect(network("203.0.113.1", self, true, true).result.getNetworkStatus().secure).toBe(
			true
		);
	});

	it("does not mistake a local SOCKS proxy for a protected remote server", () => {
		const {result} = network("127.0.0.1");
		result.proxyEnabled = true;
		expect(result.getNetworkStatus().warning).toBe("Unencrypted connection through a proxy");
		expect(result.getNetworkStatus().secure).toBe(false);
	});

	it("clears cached trust if Tailscale is stopped", async () => {
		const execute = tailscale();
		const {result} = network(peer);
		await result.refreshConnectionProtection();
		expect(result.getNetworkStatus().secure).toBe(true);
		execute.mockRestore();
		tailscale(false);
		vi.mocked(Date.now).mockReturnValue(clock + 60_000);
		await result.refreshConnectionProtection();
		expect(result.getNetworkStatus().secure).toBe(false);
	});

	it("keeps disconnected status separate from a protection warning", () => {
		const {result, transport} = network("203.0.113.1");
		transport.isConnected = () => false;
		expect(result.getNetworkStatus()).toEqual({connected: false, secure: false});
	});
});
