const HOSTED_HUGGINGFACE_HOST = /(?:^|\.)(?:huggingface\.co|hf\.co|huggingface\.cloud)$/;

/** Provider HTTP policy, not a restriction on general web access. */
export function assertHuggingfaceEndpoint(provider: string, baseUrl: string | undefined): void {
	const isHuggingface =
		provider === "huggingface" || provider === "Hugging Face" || provider === "Hugging Face Inference";
	const value = baseUrl?.trim();
	if (!value) {
		if (isHuggingface) {
			throw new Error(
				"Hugging Face requires an explicit self-hosted baseUrl; configure the existing provider base URL override. Hosted Hugging Face inference is unsupported in Mercury.",
			);
		}
		return;
	}
	let host: string;
	try {
		host = new URL(value).hostname.toLowerCase().replace(/\.$/, "");
	} catch {
		if (isHuggingface) throw new Error("Hugging Face requires a valid explicit self-hosted baseUrl.");
		return;
	}
	if (HOSTED_HUGGINGFACE_HOST.test(host)) {
		throw new Error(
			"Hosted Hugging Face provider endpoints are unsupported in Mercury; configure an explicit self-hosted baseUrl using the existing provider base URL override.",
		);
	}
}
