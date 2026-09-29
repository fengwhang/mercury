<template>
	<form id="form" method="post" action="" @submit.prevent="onSubmit">
		<span id="upload-progressbar" />
		<span id="nick">{{ network.nick }}</span>
		<label for="input" class="sr-only">Message input</label>
		<textarea
			id="input"
			ref="input"
			dir="auto"
			class="mousetrap"
			enterkeyhint="send"
			autocomplete="off"
			:value="channel.pendingMessage"
			:placeholder="getInputPlaceholder(channel)"
			@input="setPendingMessage"
			@keypress.enter.exact.prevent="onSubmit"
			@blur="onBlur"
		/>
		<span
			v-if="store.state.serverConfiguration?.fileUpload"
			id="upload-tooltip"
			class="tooltipped tooltipped-w tooltipped-no-touch"
			aria-label="Upload file"
			@click="openFileUpload"
		>
			<input
				id="upload-input"
				ref="uploadInput"
				type="file"
				aria-labelledby="upload"
				multiple
				@change="onUploadInputChange"
			/>
			<button
				id="upload"
				type="button"
				aria-label="Upload file"
				:disabled="!store.state.isConnected"
			/>
		</span>
		<span
			id="submit-tooltip"
			class="tooltipped tooltipped-w tooltipped-no-touch"
			data-tooltip="Send message"
		>
			<button
				id="submit"
				type="submit"
				aria-label="Send message"
				:disabled="!store.state.isConnected"
			/>
		</span>
	</form>
</template>

<script lang="ts">
import Mousetrap from "mousetrap";
import {wrapCursor} from "undate";
import autocompletion from "../js/autocompletion";
import {commands} from "../js/commands/index";
import socket from "../js/socket";
import upload from "../js/upload";
import eventbus from "../js/eventbus";
import {watch, defineComponent, nextTick, onMounted, PropType, ref, onUnmounted} from "vue";
import type {ClientNetwork, ClientChan} from "../js/types";
import {useStore} from "../js/store";
import {ChanType} from "../../shared/types/chan";

const formattingHotkeys = {
	"mod+k": "\x03",
	"mod+b": "\x02",
	"mod+u": "\x1F",
	"mod+i": "\x1D",
	"mod+o": "\x0F",
	"mod+s": "\x1e",
	"mod+m": "\x11",
};

// Autocomplete bracket and quote characters like in a modern IDE
// For example, select `text`, press `[` key, and it becomes `[text]`
const bracketWraps = {
	'"': '"',
	"'": "'",
	"(": ")",
	"<": ">",
	"[": "]",
	"{": "}",
	"*": "*",
	"`": "`",
	"~": "~",
	_: "_",
};

export default defineComponent({
	name: "ChatInput",
	props: {
		network: {type: Object as PropType<ClientNetwork>, required: true},
		channel: {type: Object as PropType<ClientChan>, required: true},
	},
	setup(props) {
		const store = useStore();
		const input = ref<HTMLTextAreaElement>();
		const uploadInput = ref<HTMLInputElement>();
		const autocompletionRef = ref<ReturnType<typeof autocompletion>>();

		const setInputSize = () => {
			void nextTick(() => {
				if (!input.value) {
					return;
				}

				const style = window.getComputedStyle(input.value);
				const lineHeight = parseFloat(style.lineHeight) || 1;

				// Start by resetting height before computing as scrollHeight does not
				// decrease when deleting characters
				input.value.style.height = "";

				// Use scrollHeight to calculate how many lines there are in input, and ceil the value
				// because some browsers tend to incorrently round the values when using high density
				// displays or using page zoom feature
				input.value.style.height = `${
					Math.ceil(input.value.scrollHeight / lineHeight) * lineHeight
				}px`;
			});
		};

		// Visual-row caret measurement. The textarea soft-wraps, so one logical
		// line can span several visual rows; history must trigger on the
		// first/last VISUAL row, not the first/last \n line — otherwise Up in
		// a long wrapped line yanks history instead of moving the cursor.
		// Returns null when measurement is impossible (caller falls back to
		// logical \n lines, today's behavior).
		let caretMirror: HTMLDivElement | null = null;
		const getCaretVisualRow = (
			el: HTMLTextAreaElement,
		): {row: number; total: number} | null => {
			try {
				const style = window.getComputedStyle(el);
				const lineHeight = parseFloat(style.lineHeight) || 0;
				if (!lineHeight || !el.clientWidth) {
					return null;
				}
				if (!caretMirror || !caretMirror.isConnected) {
					caretMirror = document.createElement("div");
					caretMirror.setAttribute("aria-hidden", "true");
					caretMirror.style.cssText =
						"position:absolute;top:-9999px;left:-9999px;visibility:hidden;pointer-events:none;white-space:pre-wrap;overflow-wrap:break-word;";
					document.body.appendChild(caretMirror);
				}
				const mirror = caretMirror;
				mirror.style.width = `${el.clientWidth}px`;
				for (const prop of [
					"font",
					"letterSpacing",
					"padding",
					"border",
					"boxSizing",
					"textTransform",
					"wordSpacing",
					"textIndent",
				] as const) {
					mirror.style[prop] = style[prop];
				}
				const value = el.value;
				const caret = el.selectionStart ?? value.length;
				mirror.textContent = value.slice(0, caret);
				const marker = document.createElement("span");
				marker.textContent = "\u200b";
				mirror.appendChild(marker);
				const row = Math.max(0, Math.round(marker.offsetTop / lineHeight));
				mirror.textContent = value;
				const total = Math.max(1, Math.round(mirror.scrollHeight / lineHeight));
				mirror.textContent = "";
				return {row: Math.min(row, total - 1), total};
			} catch {
				return null;
			}
		};


		const setPendingMessage = (e: Event) => {
			props.channel.pendingMessage = (e.target as HTMLInputElement).value;
			props.channel.inputHistoryPosition = 0;
			setInputSize();
		};

		const getInputPlaceholder = (channel: ClientChan) => {
			if (channel.type === ChanType.CHANNEL || channel.type === ChanType.QUERY) {
				return `Write to ${channel.name}`;
			}

			return "";
		};

		const onSubmit = () => {
			if (!input.value) {
				return;
			}

			// Triggering click event opens the virtual keyboard on mobile
			// This can only be called from another interactive event (e.g. button click)
			input.value.click();
			input.value.focus();

			if (!store.state.isConnected) {
				return false;
			}

			const target = props.channel.id;
			const text = props.channel.pendingMessage;

			if (text.length === 0) {
				return false;
			}

			if (autocompletionRef.value) {
				autocompletionRef.value.hide();
			}

			props.channel.inputHistoryPosition = 0;
			props.channel.pendingMessage = "";
			input.value.value = "";
			setInputSize();

			// Store new message in history if last message isn't already equal
			if (props.channel.inputHistory[1] !== text) {
				props.channel.inputHistory.splice(1, 0, text);
			}

			// Limit input history to a 100 entries
			if (props.channel.inputHistory.length > 100) {
				props.channel.inputHistory.pop();
			}

			if (text[0] === "/") {
				const args = text.substring(1).split(" ");
				const cmd = args.shift()?.toLowerCase();

				if (!cmd) {
					return false;
				}

				if (Object.prototype.hasOwnProperty.call(commands, cmd) && commands[cmd](args)) {
					return false;
				}
			}

			socket.emit("input", {target, text});
		};

		const onUploadInputChange = () => {
			if (!uploadInput.value || !uploadInput.value.files) {
				return;
			}

			const files = Array.from(uploadInput.value.files);
			upload.triggerUpload(files);
			uploadInput.value.value = ""; // Reset <input> element so you can upload the same file
		};

		const openFileUpload = () => {
			uploadInput.value?.click();
		};

		const blurInput = () => {
			input.value?.blur();
		};

		const onBlur = () => {
			if (autocompletionRef.value) {
				autocompletionRef.value.hide();
			}
		};

		watch(
			() => props.channel.id,
			() => {
				if (autocompletionRef.value) {
					autocompletionRef.value.hide();
				}
			}
		);

		watch(
			() => props.channel.pendingMessage,
			() => {
				setInputSize();
			}
		);

		onMounted(() => {
			eventbus.on("escapekey", blurInput);

			if (store.state.settings.autocomplete) {
				if (!input.value) {
					throw new Error("ChatInput autocomplete: input element is not available");
				}

				autocompletionRef.value = autocompletion(input.value);
			}

			const inputTrap = Mousetrap(input.value);

			inputTrap.bind(Object.keys(formattingHotkeys), function (e, key) {
				const modifier = formattingHotkeys[key];

				if (!e.target) {
					return;
				}

				wrapCursor(
					e.target as HTMLTextAreaElement,
					modifier,
					(e.target as HTMLTextAreaElement).selectionStart ===
						(e.target as HTMLTextAreaElement).selectionEnd
						? ""
						: modifier
				);

				return false;
			});

			inputTrap.bind(Object.keys(bracketWraps), function (e, key) {
				if (
					(e.target as HTMLTextAreaElement)?.selectionStart !==
					(e.target as HTMLTextAreaElement).selectionEnd
				) {
					wrapCursor(e.target as HTMLTextAreaElement, key, bracketWraps[key]);

					return false;
				}
			});

			inputTrap.bind(["up", "down"], (e, key) => {
				if (
					store.state.isAutoCompleting ||
					(e.target as HTMLTextAreaElement).selectionStart !==
						(e.target as HTMLTextAreaElement).selectionEnd ||
					!input.value
				) {
					return;
				}

				const onRow = (
					input.value.value.slice(undefined, input.value.selectionStart).match(/\n/g) ||
					[]
				).length;
				const totalRows = (input.value.value.match(/\n/g) || []).length;
				// Prefer visual rows (soft-wrap aware); fall back to logical
				// \n lines when measurement is unavailable.
				const caret = getCaretVisualRow(input.value);
				const onFirstLine = caret ? caret.row === 0 : onRow === 0;
				const onLastLine = caret ? caret.row === caret.total - 1 : onRow === totalRows;

				const {channel} = props;

				if (channel.inputHistoryPosition === 0) {
					channel.inputHistory[channel.inputHistoryPosition] = channel.pendingMessage;
				}

				if (key === "up" && onFirstLine) {
					if (channel.inputHistoryPosition < channel.inputHistory.length - 1) {
						channel.inputHistoryPosition++;
					} else {
						return;
					}
				} else if (key === "down" && channel.inputHistoryPosition > 0 && onLastLine) {
					channel.inputHistoryPosition--;
				} else {
					return;
				}

				channel.pendingMessage = channel.inputHistory[channel.inputHistoryPosition];
				input.value.value = channel.pendingMessage;
				setInputSize();

				return false;
			});

			if (store.state.serverConfiguration?.fileUpload) {
				upload.mounted();
			}
		});

		onUnmounted(() => {
			eventbus.off("escapekey", blurInput);

			if (autocompletionRef.value) {
				autocompletionRef.value.destroy();
				autocompletionRef.value = undefined;
			}

			upload.unmounted();
			upload.abort();
			if (caretMirror) {
				caretMirror.remove();
				caretMirror = null;
			}
		});

		return {
			store,
			input,
			uploadInput,
			onUploadInputChange,
			openFileUpload,
			blurInput,
			onBlur,
			setInputSize,
			upload,
			getInputPlaceholder,
			onSubmit,
			setPendingMessage,
		};
	},
});
</script>
