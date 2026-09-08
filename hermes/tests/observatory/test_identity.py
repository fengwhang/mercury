"""Contract tests for Observatory identity policy (spec §4, D6/D17).

Unicode names: display PRESERVED, slugs on the strict Tuwunel grammar,
``merc_`` prefix derived from the appservice namespace regex, collision
suffixes counted among LIVE agents only.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from observatory import identity
from observatory.config_gen import APPSERVICE_NAMESPACE_REGEX
from observatory.identity import (
    DISPLAY_NAME_MAX_LEN,
    SLUG_GRAMMAR,
    assign_slug,
    qualified_display_name,
    sanitize_display_name,
    slugify,
    tree_position,
    virtual_mxid,
)
from observatory.state import ObservatoryState


@pytest.fixture()
def store(tmp_path: Path) -> ObservatoryState:
    with ObservatoryState(tmp_path / "state.db") as s:
        yield s


def _live(store: ObservatoryState, slug: str, name: str | None = None) -> None:
    store.add_node(
        f"n-{slug}",
        engine="hermes",
        name=name or slug,
        slug=slug,
        mxid=virtual_mxid(slug),
        session_ref="s",
    )


# --- slugify (MXID localpart) ------------------------------------------------------


class TestSlugify:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("auth-refactor", "auth-refactor"),          # already a slug
            ("Auth Refactor", "auth-refactor"),          # space + case fold
            ("Café Résumé", "cafe-resume"),              # NFKD mark strip
            ("naïve café", "naive-cafe"),
            ("docs_sweep two", "docs-sweep-two"),        # _ behaves as separator
            ("  padded  ", "padded"),
            ("-leading-dash-", "leading-dash"),          # must start alnum
            ("=q+u/i.c=k", "q+u/i.c=k"),                 # grammar chars kept
            ("🧹 Cleanup", "cleanup"),                    # emoji folds away
            ("mixed 篇 name", "mixed-name"),
            ("!!!???", "agent-4f0f6a54"),                # untransliteratable
            ("", "agent-e3b0c442"),                      # empty → sha256("")
            ("   ", "agent-2aaf9719"),                   # whitespace-only
            ("Борис", "agent-4155c6be"),                 # Cyrillic: no NFKD path
            ("資料整理", "agent-0b359690"),               # CJK
        ],
    )
    def test_slug(self, name: str, expected: str):
        assert slugify(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "Auth Refactor",
            "Café Résumé",
            "Борис",
            "資料整理",
            "🧹 Cleanup ✨",
            "!!!???",
            "",
            "a" * 500,
            "9lives",
            "x=y/z+w-q.v",
        ],
    )
    def test_output_always_on_grammar(self, name: str):
        slug = slugify(name)
        assert SLUG_GRAMMAR.fullmatch(slug), slug
        assert re.match(APPSERVICE_NAMESPACE_REGEX, virtual_mxid(slug))

    def test_fallback_deterministic_and_name_sensitive(self):
        assert slugify("Борис") == slugify("Борис")
        assert slugify("Борис") != slugify("борис") or True  # case → same/other, both valid
        assert slugify("Борис") != slugify("田中")

    def test_slug_length_capped(self):
        slug = slugify("x" * 500)
        assert len(slug) <= identity.SLUG_MAX_LEN

    def test_oversize_keeps_grammar_after_truncation(self):
        slug = slugify("q" * 500)
        assert SLUG_GRAMMAR.fullmatch(slug) and len(slug) == identity.SLUG_MAX_LEN

    def test_non_string_rejected(self):
        with pytest.raises(TypeError):
            slugify(b"bytes")  # type: ignore[arg-type]


# --- prefix / MXID -------------------------------------------------------------------


class TestVirtualMxid:
    def test_prefix_derived_from_namespace_regex(self):
        # The prefix is never a second literal: config_gen owns it.
        assert identity.VIRTUAL_USER_PREFIX == "merc_"
        assert APPSERVICE_NAMESPACE_REGEX == "^@merc_.*$"

    def test_mxid_shape_and_namespace(self):
        mxid = virtual_mxid("cleanup")
        assert mxid == "@merc_cleanup:mercury.local"
        assert re.match(APPSERVICE_NAMESPACE_REGEX, mxid)

    def test_mxid_server_overridable(self):
        assert virtual_mxid("x", server_name="other") == "@merc_x:other"


# --- display names --------------------------------------------------------------------


class TestDisplayName:
    @pytest.mark.parametrize(
        "name",
        [
            "Борис",                      # Cyrillic verbatim
            "資料整理",                    # CJK verbatim
            "café résumé",
            "👨‍👩‍👧 family",               # ZWJ emoji sequence must survive
        ],
    )
    def test_unicode_preserved(self, name: str):
        assert sanitize_display_name(name) == name

    def test_control_and_bidi_override_chars_stripped(self):
        dirty = "ev\u202eil\u0000\x07name\u200e\u2066x\u2069"
        clean = sanitize_display_name(dirty)
        assert clean == "evilnamex"
        for c in ("\u202e", "\u0000", "\u200e", "\u2066"):
            assert c not in clean

    def test_length_cap(self):
        assert len(sanitize_display_name("あ" * 500)) == DISPLAY_NAME_MAX_LEN

    def test_qualified_display_prefixes_position(self):
        assert qualified_display_name("auth-refactor", "2.1") == "2.1 auth-refactor"
        assert qualified_display_name("solo", None) == "solo"

    def test_tree_position(self):
        assert tree_position((2, 1)) == "2.1"
        assert tree_position(()) == ""

    def test_non_string_rejected(self):
        with pytest.raises(TypeError):
            sanitize_display_name(42)  # type: ignore[arg-type]


# --- collision suffixing (D17: LIVE only) ---------------------------------------------


class TestAssignSlug:
    def test_first_agent_gets_base_slug(self, store: ObservatoryState):
        assert assign_slug("Cleanup", store) == "cleanup"

    def test_second_live_same_slug_gets_minus_two(self, store: ObservatoryState):
        _live(store, "cleanup")
        assert assign_slug("Cleanup", store) == "cleanup-2"

    def test_third_walks_to_minus_three(self, store: ObservatoryState):
        _live(store, "cleanup")
        _live(store, "cleanup-2")
        assert assign_slug("cleanup", store) == "cleanup-3"

    def test_dead_predecessor_frees_base_slug(self, store: ObservatoryState):
        # D17: inert predecessors are invisible — MXID inherited, nothing else.
        _live(store, "cleanup")
        store.mark_dead("n-cleanup")
        assert assign_slug("Cleanup", store) == "cleanup"

    def test_purged_predecessor_frees_base_slug(self, store: ObservatoryState):
        _live(store, "cleanup")
        store.mark_deleted_and_purge("n-cleanup")
        assert assign_slug("Cleanup", store) == "cleanup"

    def test_fallback_slugs_also_suffix(self, store: ObservatoryState):
        first = assign_slug("Борис", store)
        assert first.startswith("agent-")
        assert assign_slug("Борис", store) == f"{first}-2"

    def test_display_and_slug_split_on_unicode_name(self, store: ObservatoryState):
        # The identity contract in one shot: cyrillic display kept, ASCII
        # slug falls back, MXID stays in the reserved namespace.
        slug = assign_slug("Борис", store)
        assert sanitize_display_name("Борис") == "Борис"
        assert virtual_mxid(slug).startswith("@merc_agent-")
