import type {Client} from "irc-framework";

// Identify the fork through ISUPPORT, including remote tailnet networks.
export function isMircNetwork(client: Client): boolean {
	const options: typeof client.network.options & {MERCURY?: string} = client.network.options;
	return options.MERCURY === "1";
}
