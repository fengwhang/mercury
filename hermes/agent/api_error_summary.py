"""Provider API error classification helpers (parity port of stock ``agent/api_error_summary.py``).

Minimal closure for the ported ``turn_*`` modules: the provider stream-parse ``ValueError``
predicate. The summarising/redaction mixin surface stays in Mercury's consolidated
``run_agent.AIAgent``.
"""
import json

# Substrings of the plain ``ValueError`` jiter (the openai/anthropic SDKs' SSE JSON parser)
# raises for a truncated/corrupted event-stream frame - wire trouble, not local validation
# (#65147). Serde-style vocabulary anchored on the "at line" suffix; classify through
# ``is_provider_stream_parse_error`` rather than scanning this tuple directly.
PROVIDER_STREAM_PARSE_MARKERS = (
    "expected ident at line",
    "expected value at line",
    "eof while parsing a value at line",
    "eof while parsing a string at line",
    "eof while parsing a list at line",
    "eof while parsing an object at line",
    "key must be a string at line",
    "trailing characters at line",
    "trailing comma at line",
    "expected `,` or `}` at line",
    "expected `,` or `]` at line",
    "expected `:` at line",
    "invalid escape at line",
    "invalid number at line",
    "found while parsing a string at line",  # "control character (...U+0000-U+001F...) found while ..."
)


def is_provider_stream_parse_error(error: BaseException) -> bool:
    """True for a provider stream-parse ``ValueError`` (see ``PROVIDER_STREAM_PARSE_MARKERS``)."""
    return (isinstance(error, ValueError) and not isinstance(error, (UnicodeEncodeError, json.JSONDecodeError))
            and any(marker in str(error).lower() for marker in PROVIDER_STREAM_PARSE_MARKERS))
