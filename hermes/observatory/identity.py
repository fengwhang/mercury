"""Identity policy for Observatory virtual users (spec §4, D6/D17).

Split every agent name into its two halves:

- **Display name** — the user-chosen unicode name, preserved as-is except
  for a render-safety pass (control/bidi-override chars stripped, length
  capped). Cyrillic, CJK, emoji ZWJ sequences all survive verbatim.
- **MXID localpart** — a strict lowercase-ASCII slug on the Tuwunel
  grammar ``^[a-z0-9][a-z0-9._=/+-]*$``: NFKD-decompose, drop combining
  marks, ASCII-fold; anything that folds to nothing (CJK, Cyrillic, bare
  emoji) falls back to ``agent-<hash8>`` of the original name. Every slug
  is prefixed ``merc_`` (namespace resolution in §4) so the appservice's
  exclusive ``^@merc_.*$`` namespace covers all virtual users; the prefix
  is DERIVED from config_gen's regex constant, never re-typed here.

Collision suffixes ``-2``, ``-3``, … count LIVE same-slug agents only
(D17): inert predecessors are invisible, and a successor inherits the
base MXID with zero state inheritance.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import TYPE_CHECKING

from observatory.config_gen import (
    APPSERVICE_NAMESPACE_REGEX,
    SERVER_NAME_DEFAULT,
)

if TYPE_CHECKING:
    from observatory.state import ObservatoryState


def _prefix_from_namespace_regex(regex: str) -> str:
    """Derive the reserved localpart prefix from the appservice namespace
    regex (``"^@merc_.*$"`` → ``"merc_"``) so prefix and namespace can
    never drift apart — no second copy of the literal exists here."""
    m = re.fullmatch(r"\^@([A-Za-z0-9._=/+-]+?)(?:\.\*)?\$", regex)
    if not m:
        raise ValueError(
            f"cannot derive virtual-user prefix from namespace regex {regex!r}"
        )
    return m.group(1)


#: Reserved localpart prefix for every observatory virtual user (derived,
#: not duplicated — see :func:`_prefix_from_namespace_regex`).
VIRTUAL_USER_PREFIX = _prefix_from_namespace_regex(APPSERVICE_NAMESPACE_REGEX)


def virtual_mxid(slug: str, *, server_name: str = SERVER_NAME_DEFAULT) -> str:
    """``@merc_<slug>:<server>`` — always inside the appservice's exclusive
    user namespace (the contract test asserts this against the regex)."""
    return f"@{VIRTUAL_USER_PREFIX}{slug}:{server_name}"


# --- slug (MXID localpart) ------------------------------------------------------

#: Tuwunel localpart grammar (spec §4 / D6).
SLUG_GRAMMAR = re.compile(r"^[a-z0-9][a-z0-9._=/+-]*$")

#: Headroom under the 255-byte localpart ceiling after the ``merc_`` prefix
#: and a possible ``-NN`` collision suffix.
SLUG_MAX_LEN = 64

_SEPARATORS = re.compile(r"[\s_]+")


def _fallback_slug(name: str) -> str:
    """Deterministic identity for untransliteratable names: sha256 of the
    ORIGINAL (pre-fold) name — distinct source names never collide."""
    return f"agent-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"


def slugify(name: str) -> str:
    """Unicode name → strict lowercase-ASCII slug.

    NFKD → strip combining marks → ASCII-fold → lowercase. Runs of
    whitespace/underscore become single ``-``; characters outside the
    grammar are dropped; the result must START alphanumeric (leading /
    trailing ``._=/+-`` stripped). Empty result → ``agent-<hash8>``
    fallback (D6).
    """
    if not isinstance(name, str):
        raise TypeError(f"name must be str, got {type(name).__name__}")
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    folded = stripped.encode("ascii", "ignore").decode("ascii").lower()
    folded = _SEPARATORS.sub("-", folded)
    folded = "".join(c for c in folded if c.isalnum() or c in "._=/+-")
    folded = folded.strip("._=/+-")
    if len(folded) > SLUG_MAX_LEN:
        folded = folded[:SLUG_MAX_LEN].strip("._=/+-")
    if not folded or not SLUG_GRAMMAR.fullmatch(folded):  # grammar is a hard law
        return _fallback_slug(name)
    return folded


def assign_slug(name: str, state: "ObservatoryState") -> str:
    """Unique-among-LIVE slug for a new agent (D17).

    Base slug first; if a LIVE agent already holds it, walk ``-2``, ``-3``,
    … Dead and purged predecessors do NOT count — their MXID is inherited
    by exactly this path, with nothing else attached.
    """
    base = slugify(name)
    if not state.find_live_by_slug(base):
        return base
    n = 2
    while state.find_live_by_slug(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


# --- display name ---------------------------------------------------------------

#: Matrix caps displaynames at 256 codepoints; stay tighter — spec O5
#: leaves the exact cap to implementation, this is it.
DISPLAY_NAME_MAX_LEN = 96

#: Zero-width joiners that are legitimate inside names: ZWJ glues emoji
#: sequences (must survive), ZWNJ is load-bearing in several scripts.
_FORMAT_CHARS_KEPT = {"\u200d", "\u200c"}

#: Bidi embedding/override/isolate controls and directional marks: spoofing
#: vectors with no legitimate use in a name (spec O5 sanity rule).
_BIDI_CONTROLS = set(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    "\u200e\u200f\u206b\u206c\u206d\u206e\u206f"
)


def sanitize_display_name(name: str) -> str:
    """Render-safe display name: the user's unicode PRESERVED (Cyrillic,
    CJK, emoji incl. ZWJ sequences) minus C0/C1 controls, bidi-override
    characters and stray format chars, capped in codepoints."""
    if not isinstance(name, str):
        raise TypeError(f"name must be str, got {type(name).__name__}")
    kept = [
        c
        for c in name
        if (unicodedata.category(c) not in ("Cc", "Cf") or c in _FORMAT_CHARS_KEPT)
        and c not in _BIDI_CONTROLS
    ]
    out = "".join(kept)
    if len(out) > DISPLAY_NAME_MAX_LEN:
        out = out[:DISPLAY_NAME_MAX_LEN].rstrip() or "?"
    return out


def qualified_display_name(name: str, position: str | None = None) -> str:
    """Display name = chosen unicode name + tree position (spec §4:
    ``"2.1 auth-refactor"``). Position prefixes when present."""
    display = sanitize_display_name(name)
    return f"{position} {display}" if position else display


def tree_position(indices: list[int] | tuple[int, ...]) -> str:
    """Sibling-index path → ``"2.1"`` style label (1-based, dot-joined)."""
    return ".".join(str(i) for i in indices)
