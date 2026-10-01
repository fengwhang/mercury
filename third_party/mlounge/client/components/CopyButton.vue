<template>
	<button type="button" class="message-copy" :aria-label="label" @click="copy">
		{{ state || label }}
	</button>
</template>

<script lang="ts">
import {defineComponent, ref, watch} from "vue";
import copyText from "../js/helpers/copyText";

export default defineComponent({
	name: "CopyButton",
	props: {
		text: {type: String, required: true},
		label: {type: String, default: "Copy"},
	},
	setup(props) {
		const state = ref("");
		watch(
			() => props.text,
			() => {
				state.value = "";
			}
		);

		const copy = async () => {
			try {
				await copyText(props.text);
				state.value = "Copied";
			} catch {
				state.value = "Copy failed";
			}
		};

		return {state, copy};
	},
});
</script>
