import {createRequire} from "node:module";
import input from "../third_party/thelounge/server/plugins/inputs/msg";

// Exercise the real input handler and IRC framework splitter without a connection.
const require = createRequire(new URL("../third_party/thelounge/package.json", import.meta.url));
const {Client} = require("irc-framework");
const client = new Client({message_max_length: 40});
client.network.cap.isEnabled = () => true;
const wire: string[] = [];
client.raw = (...args: any[]) => {
  wire.push(args.length === 1 && typeof args[0] === "object" ? args[0].to1459() : args.join(" "));
};
const payload = "first\n\n" + "a".repeat(100) + "\nlast";
input.input.call({} as any, {irc: client} as any, {name: "#room"} as any, "say", [payload]);
console.log("LOUNGE INPUT", JSON.stringify(payload));
console.log("LOUNGE WIRE", JSON.stringify(wire));
