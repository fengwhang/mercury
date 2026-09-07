"""Tests for tools/omp_skills_bridge — union of both mercury skill trees.

Pinned contract (ENGINE ROOT WINS):
- omp children discover user skills ONLY as ``<agentDir>/skills/<name>/SKILL.md``
  (one level, symlinked dirs accepted), so the bridge must produce a FLAT
  namespace regardless of mercury's category nesting.
- omp's view is the UNION of its own engine-root skills and the shared
  library: real dirs/files and foreign symlinks in the engine root are
  omp's own — skipped and NEVER overwritten — and they WIN any name
  collision with the shared library (the shared copy is not linked).
- First category in sorted order wins a name collision inside the shared
  library (deterministic); the loser is never linked.
- The ``omp-managed`` category is never materialized (omp loads it via its
  own low-priority managed provider; bridging it would invert
  authored-beats-managed). Pre-fix managed links self-heal away.
- Idempotent: a no-change re-run creates/updates/removes nothing.
- Stale managed links (source vanished or newly excluded) are removed;
  managed-ness = link target resolves inside the mercury skills root.
- ``hermes.omp_skills_exclude`` in the unified config REPLACES the default
  exclude list when present.
- bridge.py ``--render-omp`` (the per-spawn path: delegate RPC children,
  /omp, cron omp_direct, post-setup sync) refreshes the union too.
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


def _reconcile(library: Path, agent: Path, **kwargs):
    return bridge.reconcile_omp_skills(
        mercury_skills_dir=library / "skills",
        omp_agent_dir=agent,
        **kwargs,
    )


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
    """omp's view = engine-root skills ∪ shared library; engine root wins."""

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


class TestRenderOmpPath:
    """bridge.py --render-omp (per-spawn) refreshes the union, fail-soft."""

    @pytest.fixture(autouse=True)
    def _layout(self, tmp_path):
        mercury = tmp_path / "mercury"
        alpha = mercury / "skills" / "alpha"
        _make_skill(alpha, "coder")
        _make_skill(alpha, "shared")
        _make_skill(mercury / "skills" / "omp-managed", "learned")
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
        # Union: engine-root native skill intact + shared skill linked.
        assert (skills / "coder").is_dir() and not (skills / "coder").is_symlink()
        assert (skills / "shared").is_symlink()
        # Managed category not promoted.
        assert not (skills / "learned").exists()

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
