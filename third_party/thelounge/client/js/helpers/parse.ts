// TODO: type
// @ts-nocheck

import {h as createElement, VNode} from "vue";
import parseStyle from "./ircmessageparser/parseStyle";
import findChannels from "./ircmessageparser/findChannels";
import {findLinks} from "../../../shared/linkify";
import findEmoji from "./ircmessageparser/findEmoji";
import findNames from "./ircmessageparser/findNames";
import merge, {MergedParts} from "./ircmessageparser/merge";
import emojiMap from "./fullnamemap.json";
import LinkPreviewToggle from "../../components/LinkPreviewToggle.vue";
import LinkPreviewFileSize from "../../components/LinkPreviewFileSize.vue";
import InlineChannel from "../../components/InlineChannel.vue";
import Username from "../../components/Username.vue";
import {ClientMessage, ClientNetwork} from "../types";
import {
	detectBlock,
	extractBlocks,
	inlineMdToIrcCodes,
	isTraceLine,
	MdBlock,
	renderCodeBlock,
	renderMathDisplay,
	renderMathSpan,
	splitInlineMath,
} from "./markdown";

const emojiModifiersRegex = /[\u{1f3fb}-\u{1f3ff}]|\u{fe0f}/gu;

type Fragment = {
	class?: string[];
	text?: string;
};

type StyledFragment = Fragment & {
	textColor?: string;
	bgColor?: string;
	hexColor?: string;
	hexBgColor?: string;

	bold?: boolean;
	italic?: boolean;
	underline?: boolean;
	monospace?: boolean;
	strikethrough?: boolean;
};

// Create an HTML `span` with styling information for a given fragment
function createFragment(fragment: StyledFragment): VNode | string | undefined {
	const classes: string[] = [];

	if (fragment.bold) {
		classes.push("irc-bold");
	}

	if (fragment.textColor !== undefined) {
		classes.push("irc-fg" + fragment.textColor);
	}

	if (fragment.bgColor !== undefined) {
		classes.push("irc-bg" + fragment.bgColor);
	}

	if (fragment.italic) {
		classes.push("irc-italic");
	}

	if (fragment.underline) {
		classes.push("irc-underline");
	}

	if (fragment.strikethrough) {
		classes.push("irc-strikethrough");
	}

	if (fragment.monospace) {
		classes.push("irc-monospace");
	}

	const data: {
		class?: string[];
		style?: Record<string, string>;
	} = {
		class: undefined,
		style: undefined,
	};

	let hasData = false;

	if (classes.length > 0) {
		hasData = true;
		data.class = classes;
	}

	if (fragment.hexColor) {
		hasData = true;
		data.style = {
			color: `#${fragment.hexColor}`,
		};

		if (fragment.hexBgColor) {
			data.style["background-color"] = `#${fragment.hexBgColor}`;
		}
	}

	return hasData ? createElement("span", data, fragment.text) : fragment.text;
}

// Transform an IRC message potentially filled with styling control codes, URLs,
// nicknames, and channels into a string of HTML elements to display on the client.
// Mercury: a server-reassembled multiline message renders as ONE row with
// real line breaks. Split first so links/channels/emoji never match across
// a line boundary, then join the per-line vnode lists with <br>.
function parse(text: string, message?: ClientMessage, network?: ClientNetwork) {
	const lines = text.split("\n");
	// Trace fast path: agent execution traces render plaintext — straight
	// to parseLine, skipping extractBlocks/detectBlock/inline-md/math. The
	// multiline join below still applies (this gates markdown only, never
	// line structure). Trace messages are single-line machine emissions;
	// gating on the first line keeps mixed content on the markdown path.
	if (lines.length > 0 && isTraceLine(lines[0])) {
		return lines.flatMap((line, index) =>
			index === 0 ? parseLine(line, message, network) : [createElement("br"), ...parseLine(line, message, network)]);
	}
	const {text: carved, blocks} = extractBlocks(text);
	return carved.split("\n").flatMap((line, index) => {
		const parts = parseRichLine(line, message, network, blocks);
		return index === 0 ? parts : [createElement("br"), ...parts];
	});
}

function parseRichLine(
	line: string,
	message?: ClientMessage,
	network?: ClientNetwork,
	blocks: MdBlock[]
) {
	const desc = detectBlock(line, blocks);

	switch (desc.type) {
		case "code":
			return [renderCodeBlock(desc.block)];
		case "math":
			return [renderMathDisplay(desc.block)];
		case "hr":
			return [createElement("div", {class: "md-hr"})];
		case "h":
			return [
				createElement(
					"div",
					{class: `md-h${desc.level}`},
					parseProse(desc.inner, message, network)
				),
			];
		case "quote":
			return [
				createElement(
					"div",
					{class: "md-quote"},
					parseProse(desc.inner, message, network)
				),
			];
		case "ul":
		case "ol":
			return [
				createElement(
					"div",
					{
						class: desc.type === "ul" ? "md-ul" : "md-ol",
						style: desc.indent > 0 ? {marginLeft: `${desc.indent}ch`} : undefined,
					},
					parseProse(desc.inner, message, network)
				),
			];
		default:
			return parseProse(line, message, network);
	}
}

function parseProse(text: string, message?: ClientMessage, network?: ClientNetwork) {
	return splitInlineMath(text).flatMap((piece) =>
		typeof piece === "string"
			? parseLine(inlineMdToIrcCodes(piece), message, network)
			: [renderMathSpan(piece.tex)]
	);
}

function parseLine(text: string, message?: ClientMessage, network?: ClientNetwork) {
	// Extract the styling information and get the plain text version from it
	const styleFragments = parseStyle(text);
	const cleanText = styleFragments.map((fragment) => fragment.text).join("");

	// On the plain text, find channels and URLs, returned as "parts". Parts are
	// arrays of objects containing start and end markers, as well as metadata
	// depending on what was found (channel or link).
	const channelPrefixes = network ? network.serverOptions.CHANTYPES : ["#", "&"];
	const userModes = network
		? network.serverOptions.PREFIX?.prefix?.map((pref) => pref.symbol)
		: ["!", "@", "%", "+"];
	const channelParts = findChannels(cleanText, channelPrefixes, userModes);
	const linkParts = findLinks(cleanText);
	const emojiParts = findEmoji(cleanText);
	const nameParts = findNames(cleanText, message ? message.users || [] : []);

	const parts = (channelParts as MergedParts)
		.concat(linkParts)
		.concat(emojiParts)
		.concat(nameParts);

	// Merge the styling information with the channels / URLs / nicks / text objects and
	// generate HTML strings with the resulting fragments
	return merge(parts, styleFragments, cleanText).map((textPart) => {
		const fragments = textPart.fragments.map((fragment) => createFragment(fragment));

		// Wrap these potentially styled fragments with links and channel buttons
		if (textPart.link) {
			const preview =
				message &&
				message.previews &&
				message.previews.find((p) => p.link === textPart.link);
			const link = createElement(
				"a",
				{
					href: textPart.link,
					dir: preview ? null : "auto",
					target: "_blank",
					rel: "noopener",
				},
				fragments
			);

			if (!preview) {
				return link;
			}

			const linkEls = [link];

			if (preview.size > 0) {
				linkEls.push(
					createElement(LinkPreviewFileSize, {
						size: preview.size,
					})
				);
			}

			linkEls.push(
				createElement(LinkPreviewToggle, {
					link: preview,
					message: message,
				})
			);

			// We wrap the link, size, and the toggle button into <span dir="auto">
			// to correctly keep the left-to-right order of these elements
			return createElement(
				"span",
				{
					dir: "auto",
				},
				linkEls
			);
		} else if (textPart.channel) {
			return createElement(
				InlineChannel,
				{
					channel: textPart.channel,
				},
				{
					default: () => fragments,
				}
			);
		} else if (textPart.emoji) {
			const emojiWithoutModifiers = textPart.emoji.replace(emojiModifiersRegex, "");
			const title = emojiMap[emojiWithoutModifiers]
				? `Emoji: ${emojiMap[emojiWithoutModifiers]}`
				: null;

			return createElement(
				"span",
				{
					class: ["emoji"],
					role: "img",
					"aria-label": title,
					title: title,
				},
				fragments
			);
		} else if (textPart.nick) {
			return createElement(
				Username,
				{
					user: {
						nick: textPart.nick,
					},
					dir: "auto",
				},
				{
					default: () => fragments,
				}
			);
		}

		return fragments;
	});
}

export default parse;
