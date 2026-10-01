// Tailnet HTTP pages may lack navigator.clipboard. Keep a selection-based
// fallback so Copy also works there, without copying rendered HTML.
export default async function copyText(text: string): Promise<void> {
	try {
		if (navigator.clipboard?.writeText) {
			await navigator.clipboard.writeText(text);
			return;
		}
	} catch {
		// Fall back when the browser denies the Clipboard API.
	}

	const focused = document.activeElement as HTMLElement | null;
	const field = document.createElement("textarea");
	field.value = text;
	field.style.position = "fixed";
	field.style.left = "-9999px";
	document.body.appendChild(field);

	try {
		field.focus();
		field.select();

		if (!document.execCommand("copy")) {
			throw new Error("Copy failed");
		}
	} finally {
		field.remove();
		focused?.focus({preventScroll: true});
	}
}
