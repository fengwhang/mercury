"""Tests for tools/omp_skills_bridge — omp's 3-source skill union.

Covers the reconcile engine (flat symlink materialization of the shared
library + hermes engine tree into omp's engine root, omp-native entries
always winning), env/dir resolution, exclude config, and the per-spawn
bridge.py --render-omp path.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import omp_skills_bridge as bridge

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BRIDGE_PY = _REPO_ROOT / "bridge" / "bridge.py"


def _make_skill(category_dir: Path, name: str, body: str = "skill") -> Path:
    skill_dir = category_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return skill_dir


@pytest.fixture()
def library(tmp_path: Path):
    """Fake mercury home: 2 categories, 3 distinct skills, 1 collision, 1 excluded."""
    mercury_home = tmp_path / "mercury"
    skills = mercury_home / "skills"
    alpha = skills / "alpha"
    beta = skills / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    _make_skill(alpha, "coder")
    _make_skill(alpha, "shared", body="from alpha")
    _make_skill(alpha, "computer-use")  # in DEFAULT_EXCLUDES
    _make_skill(beta, "websearch")
    _make_skill(beta, "shared", body="from beta")  # collision — alpha wins
    return mercury_home


@pytest.fixture()
def agent_home(tmp_path: Path):
    """Isolated omp agent dir (PI_CODING_AGENT_DIR-style override target)."""
    agent = tmp_path / "omp-agent"
    (agent / "skills").mkdir(parents=True)
    return agent


def _reconcile(library: Path, agent: Path, hermes: Path = None, **kwargs):
    return bridge.reconcile_omp_skills(
        mercury_skills_dir=library / "skills",
        hermes_skills_dir=(hermes / "skills") if hermes is not None else library / "hermes" / "skills",
        omp_agent_dir=agent,
        **kwargs,
    )


@pytest.fixture()
def hermes_tree(tmp_path: Path):
    """Fake hermes engine home ($HERMES_HOME): the tree the hermes engine
    reads — bundled-style categories with overlap against the shared
    library, engine-only skills, and one engine-side excluded skill."""
    home = tmp_path / "hermes-home"
    skills = home / "skills"
    gamma = skills / "gamma"
    delta = skills / "delta"
    gamma.mkdir(parents=True)
    delta.mkdir(parents=True)
    _make_skill(gamma, "coder", body="from gamma")          # collides with shared alpha
    _make_skill(gamma, "engine-only")                       # engine tree exclusive
    _make_skill(gamma, "openhue")                           # engine-side DEFAULT_EXCLUDES
    _make_skill(delta, "shared", body="from delta")         # collides with shared alpha
    return home


def _links(agent: Path):
    return {p.name: os.readlink(p) for p in sorted((agent / "skills").iterdir()) if p.is_symlink()}


class TestReconcile:
    def test_flat_symlinks_collision_exclude_real_dir(self, library, agent_home):
        # The user's own omp skill at a name mercury also has — must survive.
        own = agent_home / "skills" / "coder"
        own.mkdir()
        (own / "SKILL.md").write_text("---\nname: coder\n---\nmine\n", encoding="utf-8")

        summary = _reconcile(library, agent_home)

        skills = agent_home / "skills"
        # Flat symlinks created for every non-excluded, non-colliding skill.
        assert (skills / "shared").is_symlink()
        assert (skills / "websearch").is_symlink()
        # Collision: alpha (first in sorted order) wins, beta's copy never linked.
        assert (skills / "shared").resolve() == (library / "skills/alpha/shared").resolve()
        assert summary["collisions"] == ["shared"]
        # Excluded default never linked.
        assert not (skills / "computer-use").exists()
        assert "computer-use" in summary["excludes"]
        # Real dir untouched: still a dir, not a symlink, content intact.
        assert (skills / "coder").is_dir()
        assert not (skills / "coder").is_symlink()
        assert (skills / "coder" / "SKILL.md").read_text(encoding="utf-8").endswith("mine\n")
        assert summary["skipped_real"] == ["coder"]
        assert summary["created"] == 2
        assert summary["removed"] == 0
        assert summary["failed"] == []

    def test_collision_winner_is_first_sorted_category(self, tmp_path):
        skills = tmp_path / "skills"
        _make_skill(skills / "zzz", "dup")
        _make_skill(skills / "aaa", "dup")
        agent = tmp_path / "agent"

        summary = bridge.reconcile_omp_skills(
            mercury_skills_dir=skills, omp_agent_dir=agent, excludes=frozenset()
        )

        assert summary["collisions"] == ["dup"]
        assert (agent / "skills/dup").resolve() == (skills / "aaa/dup").resolve()

    def test_foreign_symlink_not_touched(self, library, agent_home):
        foreign_src = agent_home.parent / "elsewhere" / "not-ours"
        foreign_src.mkdir(parents=True)
        (foreign_src / "SKILL.md").write_text("x", encoding="utf-8")
        link = agent_home / "skills" / "shared"
        link.symlink_to(foreign_src, target_is_directory=True)

        summary = _reconcile(library, agent_home)

        assert os.readlink(link) == str(foreign_src)
        assert summary["skipped_foreign"] == ["shared"]

    def test_idempotent_no_changes_on_second_run(self, library, agent_home):
        first = _reconcile(library, agent_home)
        snapshot = _links(agent_home)

        second = _reconcile(library, agent_home)

        assert second["created"] == 0
        assert second["updated"] == 0
        assert second["removed"] == 0
        assert first["created"] == 3

    def test_stale_link_removed_when_source_disappears(self, library, agent_home):
        _reconcile(library, agent_home)
        shutil.rmtree(library / "skills/beta/websearch")
        summary = _reconcile(library, agent_home)

        assert not (agent_home / "skills/websearch").exists()
        assert summary["removed"] == 1
        # Everything else stays.
        assert (agent_home / "skills/shared").is_symlink()

    def test_excluded_skill_link_removed_on_later_run(self, library, agent_home):
        _reconcile(library, agent_home, excludes=frozenset())
        assert (agent_home / "skills/computer-use").is_symlink()

        summary = _reconcile(library, agent_home)  # defaults now exclude it

        assert not (agent_home / "skills/computer-use").exists()
        assert summary["removed"] == 1

    def test_repointed_category_updates_link(self, library, agent_home):
        _reconcile(library, agent_home)
        # "shared" moves categories: alpha's copy vanishes, beta's remains.
        shutil.rmtree(library / "skills/alpha/shared")

        summary = _reconcile(library, agent_home)

        assert summary["updated"] == 1
        assert (agent_home / "skills/shared").resolve() == (library / "skills/beta/shared").resolve()

    def test_missing_source_dir_is_empty_reconcile(self, tmp_path, agent_home):
        _reconcile(tmp_path / "no-such-library", agent_home)
        assert list((agent_home / "skills").iterdir()) == []


class TestExcludesConfig:
    def test_config_list_replaces_defaults(self, library, agent_home, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(
            "hermes:\n  omp_skills_exclude:\n    - shared\n", encoding="utf-8"
        )

        summary = _reconcile(library, agent_home, config_path=config)

        # Replaced, not extended: computer-use now bridged, shared excluded.
        assert set(summary["excludes"]) == {"shared"}
        assert (agent_home / "skills/computer-use").is_symlink()
        assert not (agent_home / "skills/shared").exists()

    def test_config_comma_string_accepted(self, library, agent_home, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(
            "hermes:\n  omp_skills_exclude: websearch, computer-use\n", encoding="utf-8"
        )
        summary = _reconcile(library, agent_home, config_path=config)
        assert set(summary["excludes"]) == {"websearch", "computer-use"}
        assert not (agent_home / "skills/websearch").exists()

    def test_invalid_shape_falls_back_to_defaults(self, library, agent_home, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(
            "hermes:\n  omp_skills_exclude:\n    ok: true\n", encoding="utf-8"
        )
        summary = _reconcile(library, agent_home, config_path=config)
        assert set(summary["excludes"]) == set(bridge.DEFAULT_EXCLUDES)

    def test_missing_config_uses_defaults(self, library, agent_home, tmp_path):
        summary = _reconcile(library, agent_home, config_path=tmp_path / "absent.yaml")
        assert set(summary["excludes"]) == set(bridge.DEFAULT_EXCLUDES)


class TestOmpAgentDirResolution:
    """Mirrors omp/packages/utils/src/dirs.ts precedence."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in ("OMP_PROFILE", "PI_PROFILE", "PI_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
            monkeypatch.delenv(var, raising=False)
        yield

    def test_default_is_dot_omp_agent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert bridge.resolve_omp_agent_dir() == tmp_path / ".omp/agent"

    def test_pi_config_dir_names_the_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PI_CONFIG_DIR", ".custom")
        assert bridge.resolve_omp_agent_dir() == tmp_path / ".custom/agent"

    def test_pi_coding_agent_dir_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "mercury/omp"))
        assert bridge.resolve_omp_agent_dir() == tmp_path / "mercury/omp"

    def test_profile_wins_over_agent_dir_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "mercury/omp"))
        monkeypatch.setenv("OMP_PROFILE", "Work")
        assert bridge.resolve_omp_agent_dir() == tmp_path / ".omp/profiles/work/agent"

    def test_pi_profile_legacy_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PI_PROFILE", "work")
        assert bridge.resolve_omp_agent_dir() == tmp_path / ".omp/profiles/work/agent"

    def test_empty_omp_profile_selects_default_not_pi_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OMP_PROFILE", "")
        monkeypatch.setenv("PI_PROFILE", "work")
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "mercury/omp"))
        assert bridge.resolve_omp_agent_dir() == tmp_path / "mercury/omp"

    def test_invalid_profile_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OMP_PROFILE", "NOT VALID!!")
        assert bridge.resolve_omp_agent_dir() == tmp_path / ".omp/agent"

    def test_relative_agent_dir_resolved_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PI_CODING_AGENT_DIR", "rel/agent")
        assert bridge.resolve_omp_agent_dir() == (tmp_path / "rel/agent").resolve()


class TestEnvResolution:
    def test_mercury_skills_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MERCURY_SKILLS_DIR", raising=False)
        monkeypatch.delenv("MERCURY_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert bridge.resolve_mercury_skills_dir() == tmp_path / ".mercury/skills"
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "m"))
        assert bridge.resolve_mercury_skills_dir() == tmp_path / "m/skills"
        monkeypatch.setenv("MERCURY_SKILLS_DIR", str(tmp_path / "custom"))
        assert bridge.resolve_mercury_skills_dir() == tmp_path / "custom"

    def test_unified_config_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MERCURY_CONFIG", raising=False)
        monkeypatch.delenv("MERCURY_HOME", raising=False)
        assert bridge.resolve_unified_config_path() is None
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
        assert bridge.resolve_unified_config_path() == tmp_path / "config.yaml"
        monkeypatch.setenv("MERCURY_CONFIG", str(tmp_path / "c.yaml"))
        assert bridge.resolve_unified_config_path() == tmp_path / "c.yaml"


class TestUnionSemantics:
    """omp's view = engine-root skills ∪ shared library; engine root wins
    (hermes-engine-tree membership covered by TestThreeSourceUnion)."""

    def test_union_engine_root_real_dir_wins_and_both_visible(self, library, agent_home):
        # omp's own engine-root skill at a name mercury also has.
        own = agent_home / "skills" / "coder"
        own.mkdir()
        (own / "SKILL.md").write_text("---\nname: coder\n---\nmine\n", encoding="utf-8")

        _reconcile(library, agent_home)

        skills = agent_home / "skills"
        # UNION: omp sees BOTH trees — its own engine-root skill ...
        assert (skills / "coder").is_dir() and not (skills / "coder").is_symlink()
        assert (skills / "coder" / "SKILL.md").read_text(encoding="utf-8").endswith("mine\n")
        # ... and every shared skill it does not shadow.
        assert (skills / "shared").is_symlink()
        assert (skills / "websearch").is_symlink()
        # ENGINE ROOT WINS: the shared copy of "coder" is NOT linked over it.
        assert (skills / "coder").resolve() != (library / "skills/alpha/coder").resolve()

    def test_managed_category_never_bridged(self, library, agent_home):
        _make_skill(library / "skills" / "omp-managed", "learned")

        summary = _reconcile(library, agent_home)

        assert not (agent_home / "skills/learned").exists()
        assert summary["created"] == 3  # coder, shared, websearch — not "learned"

    def test_pre_fix_managed_link_self_heals(self, library, agent_home):
        # A link the pre-managed-exclusion bridge created into omp-managed.
        learned = _make_skill(library / "skills" / "omp-managed", "learned")
        link = agent_home / "skills" / "learned"
        link.symlink_to(learned, target_is_directory=True)

        summary = _reconcile(library, agent_home)

        # Points inside the mercury root → managed → no longer desired → removed.
        assert not link.exists()
        assert summary["removed"] == 1


class TestThreeSourceUnion:
    """omp's view = engine root ∪ shared library ∪ hermes engine tree;
    engine root wins, shared library wins over the hermes engine tree."""

    def test_engine_tree_fills_gaps_and_shared_wins_cross_tree(self, library, hermes_tree, agent_home):
        summary = _reconcile(library, agent_home, hermes=hermes_tree)

        skills = agent_home / "skills"
        # Engine-tree-exclusive skill bridged.
        assert (skills / "engine-only").is_symlink()
        assert (skills / "engine-only").resolve() == (hermes_tree / "skills/gamma/engine-only").resolve()
        # Cross-tree collisions resolve to the SHARED library copy.
        assert (skills / "coder").resolve() == (library / "skills/alpha/coder").resolve()
        # Collisions in encounter order (within-shared first, then
        # cross-tree), deduped by name: delta/shared does not repeat it.
        assert summary["collisions"] == ["shared", "coder"]
        assert summary["sources"] == {"shared": 3, "hermes": 1}
        assert summary["created"] == 4  # coder, shared, websearch, engine-only

    def test_engine_root_real_dir_beats_both_trees(self, library, hermes_tree, agent_home):
        own = agent_home / "skills" / "coder"
        own.mkdir()
        (own / "SKILL.md").write_text("---\nname: coder\n---\nmine\n", encoding="utf-8")

        summary = _reconcile(library, agent_home, hermes=hermes_tree)

        skills = agent_home / "skills"
        assert (skills / "coder").is_dir() and not (skills / "coder").is_symlink()
        assert (skills / "coder" / "SKILL.md").read_text(encoding="utf-8").endswith("mine\n")
        assert summary["skipped_real"] == ["coder"]
        # Both other sources still visible around it.
        assert (skills / "websearch").is_symlink()      # shared
        assert (skills / "engine-only").is_symlink()    # hermes engine

    def test_engine_side_excluded_skill_not_bridged(self, library, hermes_tree, agent_home):
        summary = _reconcile(library, agent_home, hermes=hermes_tree)

        assert not (agent_home / "skills/openhue").exists()
        assert "openhue" in summary["excludes"]

    def test_stale_engine_tree_link_removed(self, library, hermes_tree, agent_home):
        _reconcile(library, agent_home, hermes=hermes_tree)
        assert (agent_home / "skills/engine-only").is_symlink()

        shutil.rmtree(hermes_tree)
        summary = _reconcile(library, agent_home, hermes=hermes_tree)

        # The link pointed inside the hermes engine root → managed → removed.
        assert not (agent_home / "skills/engine-only").exists()
        assert summary["removed"] == 1
        assert (agent_home / "skills/shared").is_symlink()

    def test_repoint_shared_to_engine_tree(self, library, hermes_tree, agent_home):
        # "shared" lives ONLY in the engine tree at first.
        shutil.rmtree(library / "skills/alpha/shared")
        shutil.rmtree(library / "skills/beta/shared")
        _reconcile(library, agent_home, hermes=hermes_tree)
        assert (agent_home / "skills/shared").resolve() == (hermes_tree / "skills/delta/shared").resolve()

        # The shared library gains the name → link repoints, engine copy loses.
        _make_skill(library / "skills/alpha", "shared", body="late from shared")
        summary = _reconcile(library, agent_home, hermes=hermes_tree)

        assert summary["updated"] == 1
        assert (agent_home / "skills/shared").resolve() == (library / "skills/alpha/shared").resolve()

    def test_aliased_roots_do_not_self_collide(self, tmp_path):
        # MERCURY_SKILLS_DIR pointed at the engine tree (same tree twice).
        skills = tmp_path / "skills"
        _make_skill(skills / "alpha", "only")
        agent = tmp_path / "agent"

        summary = bridge.reconcile_omp_skills(
            mercury_skills_dir=skills, hermes_skills_dir=skills,
            omp_agent_dir=agent, excludes=frozenset(),
        )

        assert summary["collisions"] == []
        assert summary["sources"] == {"shared": 1, "hermes": 0}
        assert summary["created"] == 1

    def test_hermes_engine_skills_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MERCURY_HOME", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert bridge.resolve_hermes_engine_skills_dir() == tmp_path / ".mercury/hermes/skills"
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
        assert bridge.resolve_hermes_engine_skills_dir() == tmp_path / "hh/skills"
        # MERCURY_HOME outranks an ambient (stock-install) HERMES_HOME.
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "m"))
        assert bridge.resolve_hermes_engine_skills_dir() == tmp_path / "m/hermes/skills"


class TestRenderOmpPath:
    """bridge.py --render-omp (per-spawn) refreshes the union, fail-soft."""

    @pytest.fixture(autouse=True)
    def _layout(self, tmp_path):
        mercury = tmp_path / "mercury"
        alpha = mercury / "skills" / "alpha"
        _make_skill(alpha, "coder")
        _make_skill(alpha, "shared")
        _make_skill(mercury / "skills" / "omp-managed", "learned")
        # The hermes engine tree the launcher forces via HERMES_HOME.
        _make_skill(mercury / "hermes" / "skills" / "research", "arxiv")
        _make_skill(mercury / "hermes" / "skills" / "devops", "openhue")  # engine-side exclude
        config = mercury / "config.yaml"
        config.write_text(
            "models:\n"
            "  default: prov/m-1\n"
            "  fallback: prov/m-2\n"
            "  delegate_model: prov/m-1\n"
            "  delegate_fallback: prov/m-2\n",
            encoding="utf-8",
        )
        agent = tmp_path / "omp-agent"
        own = agent / "skills" / "coder"  # engine-root native skill
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("---\nname: coder\n---\nmine\n", encoding="utf-8")
        self.env = {
            **os.environ,
            "HERMES_OMP_CONFIG": str(config),
            "MERCURY_CONFIG": str(config),
            "MERCURY_HOME": str(mercury),
            "HERMES_HOME": str(mercury / "hermes"),
            "MERCURY_SKILLS_DIR": str(mercury / "skills"),
            "PI_CODING_AGENT_DIR": str(agent),
            "OMP_PROFILE": "",
            "PI_PROFILE": "",
            "PI_CONFIG_DIR": "",
        }
        self.mercury = mercury
        self.agent = agent
        yield self

    def test_render_omp_refreshes_union(self):
        result = subprocess.run(
            [sys.executable, str(_BRIDGE_PY), "--render-omp"],
            capture_output=True, text=True, timeout=60, env=self.env,
        )

        assert result.returncode == 0, result.stderr
        skills = self.agent / "skills"
        # Config render still happened.
        assert "rendered omp: subtree" in result.stdout
        # Union: engine-root native skill intact + shared + hermes-engine skills.
        assert (skills / "coder").is_dir() and not (skills / "coder").is_symlink()
        assert (skills / "shared").is_symlink()
        assert (skills / "arxiv").is_symlink()
        assert (skills / "arxiv").resolve() == (self.mercury / "hermes/skills/research/arxiv").resolve()
        # Managed category not promoted; engine-side excluded skill not bridged.
        assert not (skills / "learned").exists()
        assert not (skills / "openhue").exists()

    def test_render_survives_broken_skills_tree(self):
        # A trashed skills root must never fail the config render.
        shutil.rmtree(self.mercury / "skills")

        result = subprocess.run(
            [sys.executable, str(_BRIDGE_PY), "--render-omp"],
            capture_output=True, text=True, timeout=60, env=self.env,
        )

        assert result.returncode == 0, result.stderr
        assert "rendered omp: subtree" in result.stdout
        # The engine-root native skill was never touched.
        assert (self.agent / "skills/coder/SKILL.md").read_text(encoding="utf-8").endswith("mine\n")
