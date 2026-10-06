import { describe, expect, it } from "bun:test";
import {
	encodeBase64Url,
	formatCollabLink,
	generateRoomId,
	packEnvelope,
	parseCollabLink,
	rewriteEnvelopePeer,
	unpackEnvelope,
} from "../src/lib/link";

const KEY = Uint8Array.from({ length: 32 }, (_, i) => i);
const KEY_TEXT = encodeBase64Url(KEY);
const ROOM = "AbCdEf123456_-Xy";
const RELAY = "wss://relay.example.com";

describe("collab link parsing", () => {
	it("requires a relay endpoint instead of inventing one for a bare room secret", () => {
		expect(parseCollabLink(`${ROOM}.${KEY_TEXT}`)).toEqual({
			error: "Collab link is missing a relay URL. Ask the host for a full relay.example.com/r/<roomId>.<key> link.",
		});
	});

	it("rejects legacy bare roomId#key links without a relay", () => {
		expect(parseCollabLink(`${ROOM}#${KEY_TEXT}`)).toEqual({
			error: "Collab link is missing a relay URL. Ask the host for a full relay.example.com/r/<roomId>.<key> link.",
		});
	});

	it("reports a missing relay inside a legacy browser wrapper", () => {
		expect(parseCollabLink(`https://web.example/#${ROOM}.${KEY_TEXT}`)).toEqual({
			error: "Collab link is missing a relay URL. Ask the host for a full relay.example.com/r/<roomId>.<key> link.",
		});
	});

	it("rejects upstream relay and browser endpoints without rejecting lookalike self-host names", () => {
		for (const link of [
			`wss://my.omp.sh/r/${ROOM}.${KEY_TEXT}`,
			`https://my.omp.sh/#relay.example.com/r/${ROOM}.${KEY_TEXT}`,
		]) {
			const parsed = parseCollabLink(link);
			expect("error" in parsed && parsed.error).toContain("self-hosted");
		}
		const selfHosted = parseCollabLink(`wss://my.omp.sh.example/r/${ROOM}.${KEY_TEXT}`);
		if ("error" in selfHosted) throw new Error(selfHosted.error);
		expect(selfHosted.wsUrl).toBe(`wss://my.omp.sh.example/r/${ROOM}`);
	});

	it("infers wss for scheme-less custom hosts", () => {
		const parsed = parseCollabLink(`relay.example.com:8443/r/${ROOM}#${KEY_TEXT}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`wss://relay.example.com:8443/r/${ROOM}`);
	});

	it("allows plain ws:// for localhost", () => {
		const parsed = parseCollabLink(`ws://localhost:7466/r/${ROOM}#${KEY_TEXT}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`ws://localhost:7466/r/${ROOM}`);
	});

	it("rejects plain ws:// for non-localhost hosts", () => {
		expect("error" in parseCollabLink(`ws://relay.example.com/r/${ROOM}#${KEY_TEXT}`)).toBe(true);
	});

	it("rejects keys that are not 32 or 48 base64url bytes", () => {
		const shortKey = encodeBase64Url(new Uint8Array(16));
		expect("error" in parseCollabLink(`${ROOM}#${shortKey}`)).toBe(true);
		const midKey = encodeBase64Url(new Uint8Array(40));
		expect("error" in parseCollabLink(`${ROOM}#${midKey}`)).toBe(true);
	});

	it("splits full-link fragments into key and write token", () => {
		const token = Uint8Array.from({ length: 16 }, (_, i) => 0xf0 + i);
		const full = parseCollabLink(formatCollabLink(RELAY, ROOM, KEY, token));
		if ("error" in full) throw new Error(full.error);
		expect(full.key).toEqual(KEY);
		expect(full.writeToken).toEqual(token);

		const view = parseCollabLink(formatCollabLink(RELAY, ROOM, KEY));
		if ("error" in view) throw new Error(view.error);
		expect(view.key).toEqual(KEY);
		expect(view.writeToken).toBeUndefined();
	});

	it("parses web deep links (https://<relay>/#<link>)", () => {
		const custom = parseCollabLink(`https://relay.example.com:8443/#relay.example.com:8443/r/${ROOM}#${KEY_TEXT}`);
		if ("error" in custom) throw new Error(custom.error);
		expect(custom.wsUrl).toBe(`wss://relay.example.com:8443/r/${ROOM}`);

		const local = parseCollabLink(`http://localhost:7466/#ws://localhost:7466/r/${ROOM}#${KEY_TEXT}`);
		if ("error" in local) throw new Error(local.error);
		expect(local.wsUrl).toBe(`ws://localhost:7466/r/${ROOM}`);
	});

	it("parses custom web UI wrappers by their relay fragment", () => {
		const parsed = parseCollabLink(`http://web.example/collab/#relay.example.com:8443/r/${ROOM}.${KEY_TEXT}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`wss://relay.example.com:8443/r/${ROOM}`);
	});

	it("parses split web UI wrappers with full relay URLs in the fragment", () => {
		const parsed = parseCollabLink(`https://web.example/collab/#wss://relay.example.com/r/${ROOM}.${KEY_TEXT}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`wss://relay.example.com/r/${ROOM}`);
	});

	it("falls through invalid http wrapper fragments without reparsing them", () => {
		expect(parseCollabLink("https://web.example/#not-a-collab-link")).toEqual({
			error: "Collab link must contain a /r/<roomId> path",
		});
	});

	it("prefers browser wrapper fragments over relay-like web paths", () => {
		const inner = formatCollabLink("wss://relay.example.com", ROOM, KEY);
		const parsed = parseCollabLink(`https://web.example/r/abcdefghij#${inner}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`wss://relay.example.com/r/${ROOM}`);
	});

	it("parses dot-joined web deep links (https://<relay>/#<roomId>.<key>)", () => {
		const parsed = parseCollabLink(`https://web.example/#relay.example.com/r/${ROOM}.${KEY_TEXT}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`${RELAY}/r/${ROOM}`);
		expect(parsed.key).toEqual(KEY);
	});

	it("parses legacy https direct relay links with key-only fragments", () => {
		const parsed = parseCollabLink(`https://relay.example.com/r/${ROOM}#${KEY_TEXT}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`wss://relay.example.com/r/${ROOM}`);
		expect(parsed.key).toEqual(KEY);
	});

	it("accepts %23-mangled legacy deep links (macOS Foundation re-encoding)", () => {
		const parsed = parseCollabLink(`https://web.example/#relay.example.com/r/${ROOM}%23${KEY_TEXT}`);
		if ("error" in parsed) throw new Error(parsed.error);
		expect(parsed.wsUrl).toBe(`${RELAY}/r/${ROOM}`);
		expect(parsed.key).toEqual(KEY);
	});

	it("round-trips format → parse for explicit custom and localhost relays", () => {
		const roomId = generateRoomId();
		for (const relay of [RELAY, "wss://relay.example.com:8443", "ws://127.0.0.1:7466"]) {
			const link = formatCollabLink(relay, roomId, KEY);
			const parsed = parseCollabLink(link);
			if ("error" in parsed) throw new Error(`${relay}: ${parsed.error}`);
			expect(parsed.roomId).toBe(roomId);
			expect(parsed.key).toEqual(KEY);
		}
	});
});

describe("collab wire envelope", () => {
	it("round-trips peer id and payload, rewrites in place", () => {
		const payload = new Uint8Array([1, 2, 3, 4, 5]);
		const envelope = packEnvelope(7, payload);
		const unpacked = unpackEnvelope(envelope);
		expect(unpacked?.peerId).toBe(7);
		expect(unpacked?.payload).toEqual(payload);
		rewriteEnvelopePeer(envelope, 42);
		const rewritten = unpackEnvelope(envelope);
		expect(rewritten?.peerId).toBe(42);
		expect(rewritten?.payload).toEqual(payload);
	});

	it("returns null for truncated envelopes", () => {
		expect(unpackEnvelope(new Uint8Array([0, 0]))).toBeNull();
	});
});
