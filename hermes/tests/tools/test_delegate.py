#!/usr/bin/env python3
"""
Tests for the delegation tool (omp reality).

delegate_task is a thin validator + forwarder: control actions
(list/steer/stop) run synchronously via _handle_control_action, spawn
input is validated (tasks recovery, goal presence, batch quality gate,
output_schema coercion), and the batch forwards to the omp engine
(tools.omp_delegation.dispatch_omp_delegation). The hermes-side
child-agent machinery is gone (DEAD DEPTH); tests of removed builders,
runners, depth guards, roles, credentials, and budgets were deleted with
it. What remains pins: schema shape, description contracts, validation
errors, the pause kill-switch, concurrency config, the legacy depth
shims, hidden-field stripping, and forward payload shape.

Run with:  python -m pytest tests/tools/test_delegate.py -v
"""

import json
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_BLOCKED_TOOLS,
    DELEGATE_TASK_SCHEMA,
    _get_max_concurrent_children,
    _load_config,
    delegate_task,
)
def _make_mock_parent(depth=0):
    """Create a mock parent agent with the fields delegate_task expects."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.session_id = "test-session"
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _forwarded_payload(**kwargs):
    """Run delegate_task with the omp dispatch patched; return its args."""
    from tools import omp_delegation

    captured = {}

    def fake_dispatch(parent_agent, args):
        captured["parent"] = parent_agent
        captured["args"] = dict(args)
        return json.dumps({"status": "dispatched", "engine": "omp"})

    with patch.object(omp_delegation, "dispatch_omp_delegation", fake_dispatch):
        raw = delegate_task(**kwargs)
    return json.loads(raw), captured


class TestDelegateRequirements(unittest.TestCase):

    def test_schema_valid(self):
        self.assertEqual(DELEGATE_TASK_SCHEMA["name"], "delegate_task")
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        # tasks[] is the only advertised spawn shape (single task = one-entry
        # array); legacy top-level goal/context/output_schema stay
        # handler-accepted but unadvertised.
        self.assertIn("tasks", props)
        self.assertNotIn("goal", props)
        self.assertNotIn("context", props)
        self.assertNotIn("output_schema", props)
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("goal", task_props)
        self.assertIn("context", task_props)
        self.assertIn("output_schema", task_props)
        # toolsets is intentionally NOT exposed to the model -- subagents always
        # inherit the parent's toolsets. Letting the model name toolsets was a
        # capability-selection surface the model should not control.
        self.assertNotIn("toolsets", props)
        self.assertNotIn("toolsets", props["tasks"]["items"]["properties"])
        # max_iterations is intentionally NOT exposed to the model -- it's
        # config-authoritative via delegation.max_iterations so users get
        # predictable budgets.
        self.assertNotIn("max_iterations", props)
        # ACP subprocess transport is operator-controlled via config.yaml, not
        # model-controlled via delegate_task arguments.
        self.assertNotIn("acp_command", props)
        self.assertNotIn("acp_args", props)
        self.assertNotIn("acp_command", props["tasks"]["items"]["properties"])
        self.assertNotIn("acp_args", props["tasks"]["items"]["properties"])
        self.assertNotIn("maxItems", props["tasks"])  # removed -- limit is now runtime-configurable

    def test_top_level_description_compact_and_complete(self):
        """The top-level description must stay compact while keeping every
        contract that exists nowhere else in the schema (keyword-level, not
        prose-literal, so rewording doesn't break CI)."""
        from tools.delegate_tool import _build_top_level_description

        desc = _build_top_level_description()
        # Compaction ceiling: the old description was ~4,000 chars.
        self.assertLessEqual(len(desc), 2200)
        # Contracts only the top-level text carries:
        for keyword in (
            "background",          # async semantics
            "wait or poll",        # no-poll rule
            "execute_code",        # mechanical-work routing
            "cronjob",             # durable-work routing
            "/stop",               # non-durability warning
            "context",             # pass-everything-via-context rule
            "respond in Chinese",  # language example (weak models regress without it)
            "SELF-REPORTS",        # verification contract
            "clarify",             # child blocked-tool list
            "delegate slots",      # model inheritance / pinning
        ):
            self.assertIn(keyword, desc, f"top-level description lost: {keyword!r}")
        # send_message must NOT be named: gateway-internal vocabulary most
        # sessions never see (still enforced via DELEGATE_BLOCKED_TOOLS).
        self.assertNotIn("send_message", desc)

    def test_dynamic_limits_moved_to_param_descriptions(self):
        """Concurrency reaches the model through the tasks parameter
        description; no role param is advertised and no depth text exists
        (DEAD DEPTH -- recursion is omp-side)."""
        from tools.delegate_tool import _build_dynamic_schema_overrides
        from tools.registry import registry

        with patch(
            "tools.delegate_tool._get_max_concurrent_children", return_value=7
        ):
            overrides = _build_dynamic_schema_overrides()
            definition = registry.get_definitions({"delegate_task"})[0]["function"]

        for parameters in (overrides["parameters"], definition["parameters"]):
            self.assertIn("up to 7", parameters["properties"]["tasks"]["description"])
            self.assertNotIn("role", parameters["properties"])
        self.assertNotIn("up to 7", overrides["description"])
        self.assertNotIn("max_spawn_depth", overrides["description"])


class TestDelegateTaskValidation(unittest.TestCase):
    def test_no_parent_agent(self):
        result = json.loads(delegate_task(goal="test"))
        self.assertIn("error", result)
        self.assertIn("parent agent", result["error"])

    def test_no_tasks_provided(self):
        parent = _make_mock_parent()
        result = json.loads(delegate_task(parent_agent=parent))
        self.assertIn("error", result)
        self.assertIn("No tasks provided", result["error"])

    def test_missing_goal_in_task(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(tasks=[{"goal": "  ", "name": "empty"}], parent_agent=parent)
        )
        self.assertIn("error", result)
        self.assertIn("missing a 'goal'", result["error"])

    def test_non_dict_task_rejected(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(tasks=["just a string"], parent_agent=parent)
        )
        self.assertIn("error", result)
        self.assertIn("must be an object", result["error"])

    def test_too_many_tasks(self):
        parent = _make_mock_parent()
        with patch(
            "tools.delegate_tool._get_max_concurrent_children", return_value=2
        ):
            result = json.loads(
                delegate_task(
                    tasks=[
                        {"goal": "first task here", "name": "one"},
                        {"goal": "second task here", "name": "two"},
                        {"goal": "third task here", "name": "three"},
                    ],
                    parent_agent=parent,
                )
            )
        self.assertIn("error", result)
        self.assertIn("Too many tasks", result["error"])

    def test_placeholder_goal_rejected(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(
                tasks=[
                    {"goal": "TODO", "name": "one"},
                    {"goal": "do the real thing please", "name": "two"},
                ],
                parent_agent=parent,
            )
        )
        self.assertIn("error", result)
        self.assertIn("placeholder", result["error"])

    def test_template_marker_rejected(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(
                tasks=[
                    {"goal": "migrate <feature_name> now please", "name": "one"},
                    {"goal": "do the real thing please", "name": "two"},
                ],
                parent_agent=parent,
            )
        )
        self.assertIn("error", result)
        self.assertIn("template marker", result["error"])

    def test_short_batch_goal_rejected(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(
                tasks=[
                    {"goal": "fix it", "name": "one"},
                    {"goal": "do the real thing please", "name": "two"},
                ],
                parent_agent=parent,
            )
        )
        self.assertIn("error", result)
        self.assertIn("too short", result["error"])

    def test_short_single_goal_allowed(self):
        # A single task legitimately uses short goals ("Fix the tests").
        result, captured = _forwarded_payload(
            goal="Fix it", parent_agent=_make_mock_parent()
        )
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(captured["args"]["tasks"][0]["goal"], "Fix it")

    def test_json_string_tasks_recovered(self):
        result, captured = _forwarded_payload(
            tasks=json.dumps(
                [{"goal": "do the thing properly", "name": "thing"}]
            ),
            parent_agent=_make_mock_parent(),
        )
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(
            captured["args"]["tasks"][0]["goal"], "do the thing properly"
        )

    def test_malformed_json_string_tasks_rejected(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(tasks="{not json", parent_agent=parent)
        )
        self.assertIn("error", result)

    def test_invalid_output_schema_rejected(self):
        parent = _make_mock_parent()
        # {'type': 'bogus-type'} passes coerce (it is not a validator), so
        # use a non-dict schema, which coercion rejects loudly instead of
        # dispatching a child that can never satisfy its contract.
        result = json.loads(
            delegate_task(
                tasks=[
                    {
                        "goal": "do the thing properly",
                        "name": "thing",
                        "output_schema": "bogus-type",
                    }
                ],
                parent_agent=parent,
            )
        )
        self.assertIn("error", result)
        self.assertIn("output_schema invalid", result["error"])

    def test_empty_tasks_array_falls_back_to_goal(self):
        result, captured = _forwarded_payload(
            goal="Fix the tests", tasks=[], parent_agent=_make_mock_parent()
        )
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(captured["args"]["tasks"]), 1)


class TestDelegateTaskForwarding(unittest.TestCase):
    def test_spawn_forwards_named_tasks_to_omp(self):
        result, captured = _forwarded_payload(
            tasks=[
                {"goal": "first real task here", "name": "first"},
                {"goal": "second real task here"},
            ],
            parent_agent=_make_mock_parent(),
        )
        self.assertEqual(result["engine"], "omp")
        forwarded = captured["args"]["tasks"]
        self.assertEqual(forwarded[0]["name"], "first")
        # Missing names get the derived fallback (handler seam, D6).
        self.assertEqual(forwarded[1]["name"], "task-2")

    def test_hidden_acp_fields_stripped_before_forward(self):
        result, captured = _forwarded_payload(
            tasks=[
                {
                    "goal": "do the thing properly",
                    "name": "thing",
                    "acp_command": "claude",
                    "acp_args": ["--acp"],
                }
            ],
            parent_agent=_make_mock_parent(),
        )
        self.assertEqual(result["status"], "dispatched")
        task = captured["args"]["tasks"][0]
        self.assertNotIn("acp_command", task)
        self.assertNotIn("acp_args", task)

    def test_legacy_knobs_accepted_and_ignored(self):
        # role / max_iterations / credentials_cfg / background are wire
        # compat only: accepted, never forwarded to the omp engine.
        result, captured = _forwarded_payload(
            goal="do the thing properly",
            role="orchestrator",
            max_iterations=5,
            credentials_cfg={"provider": "openrouter"},
            background=True,
            parent_agent=_make_mock_parent(),
        )
        self.assertEqual(result["status"], "dispatched")
        self.assertNotIn("role", captured["args"])
        self.assertNotIn("max_iterations", captured["args"])
        self.assertNotIn("credentials_cfg", captured["args"])
        self.assertNotIn("background", captured["args"])

    def test_omp_failure_surfaces(self):
        from tools import omp_delegation

        with patch.object(
            omp_delegation,
            "dispatch_omp_delegation",
            return_value=json.dumps({"error": "omp engine down"}),
        ):
            result = json.loads(
                delegate_task(
                    goal="do the thing properly",
                    parent_agent=_make_mock_parent(),
                )
            )
        self.assertIn("error", result)


class TestDelegateTaskControl(unittest.TestCase):
    def test_list_routes_to_control_plane_not_omp(self):
        from tools import omp_delegation

        parent = _make_mock_parent()
        with patch.object(
            omp_delegation,
            "dispatch_omp_delegation",
            side_effect=AssertionError("must not dispatch"),
        ):
            result = json.loads(
                delegate_task(action="list", parent_agent=parent)
            )
        self.assertEqual(result["action"], "list")
        self.assertEqual(result["count"], 0)

    def test_unknown_action_rejected(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(action="bogus", parent_agent=parent)
        )
        self.assertIn("error", result)
        self.assertIn("Unknown action", result["error"])

    def test_steer_without_id_rejected(self):
        parent = _make_mock_parent()
        result = json.loads(
            delegate_task(action="steer", message="go faster", parent_agent=parent)
        )
        self.assertIn("error", result)
        self.assertIn("subagent_id", result["error"])


class TestPauseKillSwitch(unittest.TestCase):
    def test_paused_spawn_refused_list_still_works(self):
        from tools.delegate_tool import is_spawn_paused, set_spawn_paused

        parent = _make_mock_parent()
        old = is_spawn_paused()
        set_spawn_paused(True)
        try:
            self.assertTrue(is_spawn_paused())
            result = json.loads(
                delegate_task(
                    goal="do the thing properly", parent_agent=parent
                )
            )
            self.assertIn("error", result)
            self.assertIn("paused", result["error"].lower())
            # Control plane bypasses the pause gate.
            listed = json.loads(
                delegate_task(action="list", parent_agent=parent)
            )
            self.assertEqual(listed["action"], "list")
        finally:
            set_spawn_paused(old)

    def test_pause_roundtrip(self):
        from tools.delegate_tool import is_spawn_paused, set_spawn_paused

        old = is_spawn_paused()
        try:
            set_spawn_paused(True)
            self.assertTrue(is_spawn_paused())
            set_spawn_paused(False)
            self.assertFalse(is_spawn_paused())
        finally:
            set_spawn_paused(old)


class TestConcurrencyDefaults(unittest.TestCase):
    """Tests for the concurrency default and no hard ceiling."""

    def test_load_config_prefers_active_persistent_config_over_cli_defaults(self):
        stale_cli = types.ModuleType("cli")
        stale_cli.CLI_CONFIG = {
            "delegation": {
                "max_iterations": 45,
                "model": "",
                "provider": "",
                "base_url": "",
                "api_key": "",
            }
        }
        active_config = {
            "delegation": {
                "max_iterations": 50,
                "max_concurrent_children": 50,
                "max_spawn_depth": 10,
            }
        }

        with patch.dict("sys.modules", {"cli": stale_cli}):
            with patch(
                "mercury_cli.config.load_config_readonly", return_value=active_config
            ):
                self.assertEqual(_load_config()["max_concurrent_children"], 50)
                self.assertEqual(_get_max_concurrent_children(), 50)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 0})
    def test_zero_clamped_to_one(self, mock_cfg):
        """Floor of 1 is enforced; zero or negative values raise to 1."""
        self.assertEqual(_get_max_concurrent_children(), 1)


class TestAsyncCapUnified(unittest.TestCase):
    """max_async_children is deprecated: the async cap IS max_concurrent_children."""

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 15})
    def test_async_cap_follows_concurrent_children(self, mock_cfg):
        from tools.delegate_tool import _get_max_async_children
        self.assertEqual(_get_max_async_children(), 15)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 15, "max_async_children": 3})
    def test_stale_max_async_children_ignored(self, mock_cfg):
        """A leftover max_async_children in config must not shrink the cap."""
        from tools.delegate_tool import _get_max_async_children
        self.assertEqual(_get_max_async_children(), 15)


class TestLegacyDepthShim(unittest.TestCase):
    """The hermes-side depth guard is gone; the shim stays value-compatible."""

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_max_spawn_depth_defaults_to_1(self, mock_cfg):
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 1)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 0})
    def test_max_spawn_depth_floored_at_one(self, mock_cfg):
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 1)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": "bogus"})
    def test_max_spawn_depth_invalid_falls_back_to_1(self, mock_cfg):
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 1)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_orchestrator_enabled_defaults_true(self, mock_cfg):
        from tools.delegate_tool import _get_orchestrator_enabled
        self.assertTrue(_get_orchestrator_enabled())

    @patch("tools.delegate_tool._load_config",
           return_value={"orchestrator_enabled": "false"})
    def test_orchestrator_enabled_parses_strings(self, mock_cfg):
        from tools.delegate_tool import _get_orchestrator_enabled
        self.assertFalse(_get_orchestrator_enabled())

    def test_no_depth_limit_on_spawn(self):
        # Any _delegate_depth dispatches: depth never gates the omp engine.
        result, _ = _forwarded_payload(
            goal="do the thing properly",
            parent_agent=_make_mock_parent(depth=5),
        )
        self.assertEqual(result["status"], "dispatched")


class TestRoleParamRetired(unittest.TestCase):
    def test_schema_no_longer_advertises_role(self):
        """`role` left the advertised schema; the handler still accepts it."""
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertNotIn("role", props)
        self.assertNotIn("role", props["tasks"]["items"]["properties"])

    def test_schema_omits_acp_transport_fields(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]

        task_props = props["tasks"]["items"]["properties"]
        self.assertNotIn("acp_command", props)
        self.assertNotIn("acp_args", props)
        self.assertNotIn("acp_command", task_props)
        self.assertNotIn("acp_args", task_props)

    def test_role_param_description_is_legacy_note(self):
        from tools.delegate_tool import _build_role_param_description

        desc = _build_role_param_description()
        self.assertIn("Legacy", desc)
        self.assertIn("omp", desc)


class TestBlockedTools(unittest.TestCase):
    def test_blocked_set_still_names_child_denied_tools(self):
        # The omp child allowlist rule in the top-level description is
        # grounded here: these tools stay out of delegated children.
        for name in ("delegate_task", "clarify", "memory", "cronjob"):
            self.assertIn(name, DELEGATE_BLOCKED_TOOLS)

    def test_execute_code_not_blocked(self):
        self.assertNotIn("execute_code", DELEGATE_BLOCKED_TOOLS)


class TestHiddenTaskFields(unittest.TestCase):
    def test_strip_removes_acp_transport_keys(self):
        from tools.delegate_tool import _strip_model_hidden_task_fields

        tasks = [{"goal": "g", "acp_command": "c", "acp_args": ["a"]}]
        stripped = _strip_model_hidden_task_fields(tasks)
        self.assertEqual(stripped, [{"goal": "g"}])

    def test_strip_leaves_clean_tasks_untouched(self):
        from tools.delegate_tool import _strip_model_hidden_task_fields

        tasks = [{"goal": "g", "name": "n"}]
        self.assertIs(_strip_model_hidden_task_fields(tasks), tasks)

    def test_strip_non_list_passthrough(self):
        from tools.delegate_tool import _strip_model_hidden_task_fields

        self.assertIsNone(_strip_model_hidden_task_fields(None))

    def test_model_background_value_top_level_is_background(self):
        from tools.delegate_tool import _model_background_value

        self.assertTrue(_model_background_value({}, _make_mock_parent(depth=0)))

    def test_model_background_value_subagent_is_sync(self):
        from tools.delegate_tool import _model_background_value

        self.assertFalse(_model_background_value({}, _make_mock_parent(depth=1)))


class TestValidationHelpers(unittest.TestCase):
    def test_normalize_delegation_names_fills_fallbacks(self):
        from tools.delegate_tool import normalize_delegation_names

        tasks = [{"goal": "a"}, {"goal": "b", "name": "  kept  "}]
        normalize_delegation_names(tasks)
        self.assertEqual(tasks[0]["name"], "task-1")
        self.assertEqual(tasks[1]["name"], "kept")

    def test_recover_tasks_from_json_string(self):
        from tools.delegate_tool import _recover_tasks_from_json_string

        parsed, err = _recover_tasks_from_json_string(
            json.dumps([{"goal": "g"}])
        )
        self.assertIsNone(err)
        self.assertEqual(parsed, [{"goal": "g"}])
        self.assertEqual(_recover_tasks_from_json_string(None), (None, None))
        _, err = _recover_tasks_from_json_string("{bad")
        self.assertIsNotNone(err)

    def test_validate_batch_tasks_accepts_single_short_goal(self):
        from tools.delegate_tool import _validate_batch_tasks

        self.assertIsNone(_validate_batch_tasks([{"goal": "Fix it"}]))

    def test_removed_engine_shims_raise(self):
        import tools.delegate_tool as dt

        for name in (
            "_build_child_agent",
            "_build_child_system_prompt",
            "_build_child_progress_callback",
            "_build_child_preserving_parent_tools",
            "_run_single_child",
            "_run_child_lifecycle",
            "_finalize_child_results",
            "_strip_blocked_tools",
            "_blocked_toolsets_for_role",
            "_resolve_delegation_credentials",
            "_resolve_child_credential_pool",
            "_merge_request_overrides",
        ):
            with self.assertRaises(RuntimeError, msg=name):
                getattr(dt, name)()


class TestDispatchDelegateTask(unittest.TestCase):
    """Tests for the _dispatch_delegate_task helper and full param forwarding."""

    def test_model_acp_args_not_forwarded(self):
        """The live model dispatch path strips hidden ACP transport args.

        MERCURY-OMP PATCH (B1): dispatch routes to the omp engine; the strip
        invariant is unchanged -- hidden transport args must not reach the
        delegation engine (which composes the omp prompt verbatim).
        """
        import run_agent

        captured = {}

        def fake_omp_dispatch(parent_agent, args):
            captured.update(args)
            return "{}"

        parent = _make_mock_parent(depth=0)
        with patch("tools.omp_delegation.dispatch_omp_delegation",
                   fake_omp_dispatch):
            run_agent.AIAgent._dispatch_delegate_task(
                parent,
                {
                    "goal": "test",
                    "acp_command": "claude",
                    "acp_args": ["--acp", "--stdio"],
                    "tasks": [
                        {
                            "goal": "nested",
                            "acp_command": "codex",
                            "acp_args": ["--acp"],
                        },
                    ],
                },
            )

        self.assertEqual(captured["goal"], "test")
        self.assertNotIn("acp_command", captured)
        self.assertNotIn("acp_args", captured)


if __name__ == "__main__":
    unittest.main()
