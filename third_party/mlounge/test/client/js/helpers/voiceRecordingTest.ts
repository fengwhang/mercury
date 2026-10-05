import {afterEach, describe, expect, it, vi} from "vitest";
import {recordVoiceSegments} from "../../../../client/js/helpers/voice-recording";

class Recorder {
	// eslint-disable-next-line no-use-before-define -- Recursive type of this test recorder.
	static instances: Recorder[] = [];
	state = "inactive";
	mimeType = "audio/mp4";
	ondataavailable?: (event: {data: Blob}) => void;
	onstop?: () => void;
	onerror?: () => void;
	start = vi.fn(() => {
		this.state = "recording";
	});
	constructor() {
		Recorder.instances.push(this);
	}
	stop() {
		this.state = "inactive";
		this.ondataavailable?.({data: new Blob(["complete-file"])});
		this.onstop?.();
	}
}

afterEach(() => {
	vi.unstubAllGlobals();
	vi.useRealTimers();
	Recorder.instances = [];
});

describe("voice recorder segments", () => {
	it("waits for a speech pause before finalizing an independent audio file", () => {
		vi.useFakeTimers();
		vi.stubGlobal("MediaRecorder", Recorder);
		let speaking = true;
		const chunks: Blob[] = [];
		const recording = recordVoiceSegments(
			{} as MediaStream,
			"audio/mp4",
			(blob) => chunks.push(blob),
			2000,
			{
				canFinalize: () => !speaking,
			}
		);
		vi.advanceTimersByTime(4000);
		expect(chunks).toHaveLength(0);
		speaking = false;
		vi.advanceTimersByTime(100);
		expect(chunks).toHaveLength(1);
		recording.stop();
	});
	it("reports asynchronous recording failure and stops producing segments", () => {
		vi.useFakeTimers();
		vi.stubGlobal("MediaRecorder", Recorder);
		const chunks = vi.fn();
		const error = vi.fn();
		const recording = recordVoiceSegments({} as MediaStream, "audio/mp4", chunks, 2000, {
			onError: error,
		});
		Recorder.instances[0].onerror?.();
		vi.advanceTimersByTime(5000);
		expect(error).toHaveBeenCalledOnce();
		expect(chunks).not.toHaveBeenCalled();
		recording.stop();
	});
	it("finalizes each file before sending it and stops producing audio on hangup", async () => {
		vi.useFakeTimers();
		vi.stubGlobal("MediaRecorder", Recorder);
		const chunks: Blob[] = [];
		const recording = recordVoiceSegments({} as MediaStream, "audio/mp4", (blob) =>
			chunks.push(blob)
		);
		vi.advanceTimersByTime(4000);
		expect(chunks).toHaveLength(2);
		expect(chunks.map((b) => b.type)).toEqual(["audio/mp4", "audio/mp4"]);
		expect(await chunks[1].text()).toBe("complete-file");
		expect(Recorder.instances).toHaveLength(3);

		for (const recorder of Recorder.instances) {
			expect(recorder.start).toHaveBeenCalledWith();
		}

		recording.stop();
		vi.advanceTimersByTime(10000);
		expect(chunks).toHaveLength(2);
		expect(Recorder.instances[2].state).toBe("inactive");
	});
});
