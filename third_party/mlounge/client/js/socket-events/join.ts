import socket from "../socket";
import {store} from "../store";
import {switchToChannel} from "../router";
import {ClientChan} from "../types";
import {toClientChan} from "../chan";

socket.on("join", function (data) {
	const network = store.getters.findNetwork(data.network);

	if (!network) {
		return;
	}

	const clientChan: ClientChan = toClientChan(data.chan);
	network.channels.splice(data.index || -1, 0, clientChan);

	// Mercury: auto-joined rooms (agent rooms, spawn invites) land in the
	// sidebar without stealing focus — respect shouldOpen for every
	// channel type, not just queries (upstream only guards queries).
	if (!data.shouldOpen) {
		return;
	}

	const chan = store.getters.findChannel(data.chan.id);

	if (chan) {
		switchToChannel(chan.channel);
	} else {
		// eslint-disable-next-line no-console
		console.error("Could not find channel", data.chan.id);
	}
});
