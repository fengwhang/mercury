// A MediaRecorder timeslice is a fragment of one container. Starting a fresh
// recording after each stop gives STT an independently decodable file.
export function recordVoiceSegments(
	stream: MediaStream,
	mime: string,
	onChunk: (blob: Blob) => void,
	duration = 2000,
	options: {
		canFinalize?: () => boolean;
		maximumDuration?: number;
		onError?: (error: Error) => void;
	} = {}
): {stop: () => void} {
	let stopped = false;
	let timer: ReturnType<typeof setTimeout>;
	let recorder: MediaRecorder;

	const fail = (error: Error) => {
		stopped = true;
		clearTimeout(timer);
		options.onError?.(error);
	};

	const start = () => {
		if (stopped) {
			return;
		}

		recorder = mime ? new MediaRecorder(stream, {mimeType: mime}) : new MediaRecorder(stream);
		const chunks: Blob[] = [];

		recorder.ondataavailable = (event) => {
			if (event.data.size) {
				chunks.push(event.data);
			}
		};

		recorder.onstop = () => {
			if (!stopped) {
				onChunk(new Blob(chunks, {type: recorder.mimeType || mime}));

				try {
					start();
				} catch (error) {
					fail(error instanceof Error ? error : new Error(String(error)));
				}
			}
		};

		recorder.onerror = () => fail(new Error("Microphone recording failed."));
		recorder.start();
		const started = Date.now();

		const finalize = () => {
			if (stopped) {
				return;
			}

			if (
				options.canFinalize &&
				!options.canFinalize() &&
				Date.now() - started < (options.maximumDuration || 12000)
			) {
				timer = setTimeout(finalize, 100);
				return;
			}

			try {
				recorder.stop();
			} catch (error) {
				fail(error instanceof Error ? error : new Error(String(error)));
			}
		};

		timer = setTimeout(finalize, duration);
	};

	start();
	return {
		stop() {
			stopped = true;
			clearTimeout(timer);

			if (recorder.state !== "inactive") {
				recorder.stop();
			}
		},
	};
}
