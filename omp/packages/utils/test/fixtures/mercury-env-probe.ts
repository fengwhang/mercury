import { $env } from "@oh-my-pi/pi-utils/env";

process.stdout.write(
	JSON.stringify({
		marker: $env.MERCURY_CASCADE_PROBE ?? null,
		override: $env.MERCURY_CASCADE_OVERRIDE ?? null,
	}),
);
