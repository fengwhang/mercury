<template>
	<div class="message-body">
		<span class="message-tools">
			<button type="button" :aria-pressed="raw" @click="toggleRaw">Raw</button>
		</span>
		<pre v-if="raw" class="message-raw">{{ message.text }}</pre>
		<ParsedMessage v-else :network="network" :message="message" />
	</div>
</template>

<script lang="ts">
import {defineComponent, PropType, ref, nextTick} from "vue";
import type {ClientMessage, ClientNetwork} from "../js/types";
import ParsedMessage from "./ParsedMessage.vue";

export default defineComponent({
	name: "MessageBody",
	components: {ParsedMessage},
	props: {
		message: {type: Object as PropType<ClientMessage>, required: true},
		network: Object as PropType<ClientNetwork>,
		keepScrollPosition: Function as PropType<() => void>,
	},
	setup(props) {
		const raw = ref(false);

		const toggleRaw = async () => {
			props.keepScrollPosition?.();
			raw.value = !raw.value;
			await nextTick();
		};

		return {raw, toggleRaw};
	},
});
</script>
