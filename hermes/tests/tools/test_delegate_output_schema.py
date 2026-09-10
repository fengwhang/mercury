"""T1-24: structured-output schema on delegate_task (omp reality).

Per-task ``output_schema`` (JSON Schema object): the schema is coerced at
dispatch (malformed schemas fail loudly before any spawn) and forwarded to
the omp engine, which renders it into the child prompt via
``_build_task_prompt``. The hermes-side child runner that used to validate
answers and send a bounded retry turn is gone (DEAD DEPTH); there are no
``schema_valid`` / ``schema_errors`` result keys hermes-side. Omp-side
conformance is prompt-level (best-effort JSON instruction), not a
validated retry loop.

Pattern from: github/copilot-cli ctx.agent(prompt, {schema}) — PATTERN
ONLY, zero code/prompt text copied (proprietary).
"""

import json
import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    delegate_task,
)
from tools.delegation_output_schema import (
    append_output_contract,
    build_retry_message,
    coerce_output_schema,
    validate_output,
)

ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "zip": {"type": "string"},
    },
    "required": ["city"],
}


# ---------------------------------------------------------------------------
# Helper-module unit tests
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_valid_json_matching_schema(self):
        ok, errors = validate_output('{"city": "Berlin"}', ADDRESS_SCHEMA)
        assert ok is True
        assert errors == []

    def test_json_violating_schema_reports_errors(self):
        ok, errors = validate_output('{"zip": "10115"}', ADDRESS_SCHEMA)
        assert ok is False
        assert errors
        assert any("city" in e for e in errors)

    def test_non_json_text_reports_parse_error(self):
        ok, errors = validate_output("I could not produce JSON, sorry.", ADDRESS_SCHEMA)
        assert ok is False
        assert errors

    def test_code_fenced_json_is_accepted(self):
        text = '```json\n{"city": "Oslo"}\n```'
        ok, errors = validate_output(text, ADDRESS_SCHEMA)
        assert ok is True
        assert errors == []

    def test_json_embedded_in_prose_is_extracted(self):
        text = 'Here is the result:\n{"city": "Lima"}\nHope that helps!'
        ok, _ = validate_output(text, ADDRESS_SCHEMA)
        assert ok is True

    def test_empty_text_is_invalid(self):
        ok, errors = validate_output("", ADDRESS_SCHEMA)
        assert ok is False
        assert errors


class TestCoerceOutputSchema:
    def test_valid_schema_passes(self):
        schema, err = coerce_output_schema(ADDRESS_SCHEMA)
        assert schema == ADDRESS_SCHEMA
        assert err is None

    def test_none_passes_through(self):
        schema, err = coerce_output_schema(None)
        assert schema is None
        assert err is None

    def test_non_dict_is_rejected(self):
        schema, err = coerce_output_schema("not-a-schema")
        assert schema is None
        assert err

    def test_invalid_json_schema_is_rejected(self):
        schema, err = coerce_output_schema({"type": 42})
        assert schema is None
        assert err


class TestPromptPlumbing:
    def test_contract_block_carries_schema(self):
        out = append_output_contract("base context", ADDRESS_SCHEMA)
        assert "base context" in out
        assert "OUTPUT CONTRACT" in out
        assert '"city"' in out

    def test_contract_block_without_prior_context(self):
        out = append_output_contract(None, ADDRESS_SCHEMA)
        assert "OUTPUT CONTRACT" in out

    def test_retry_message_carries_verbatim_errors(self):
        msg = build_retry_message(["'city' is a required property"])
        assert "'city' is a required property" in msg
        assert "JSON" in msg


# ---------------------------------------------------------------------------
# Tool-schema surface (one-time static field)
# ---------------------------------------------------------------------------


class TestToolSchemaSurface:
    def test_output_schema_on_task_items(self):
        item_props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"][
            "items"
        ]["properties"]
        assert "output_schema" in item_props
        assert item_props["output_schema"]["type"] == "object"
        # never required
        assert "output_schema" not in DELEGATE_TASK_SCHEMA["parameters"][
            "properties"
        ]["tasks"]["items"]["required"]

    def test_output_schema_advertised_per_task_only(self):
        """output_schema is advertised inside tasks[] items (the only spawn
        shape); the legacy top-level param stays handler-accepted but out
        of the schema."""
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        assert "output_schema" not in props
        task_props = props["tasks"]["items"]["properties"]
        assert task_props["output_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# delegate_task dispatch-time schema handling (omp forward)
# ---------------------------------------------------------------------------


def _make_mock_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


class TestDelegateTaskDispatch:
    def test_non_dict_output_schema_rejected(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            out = delegate_task(
                tasks=[
                    {"goal": "Summarize the release notes for module A", "output_schema": "not-a-dict"},
                    {"goal": "Summarize the release notes for module B"},
                ],
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert payload.get("error")
        assert "output_schema" in payload["error"]

    def test_invalid_json_schema_rejected_at_dispatch(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            out = delegate_task(
                tasks=[
                    {"goal": "Summarize the release notes for module A", "output_schema": {"type": 42}},
                    {"goal": "Summarize the release notes for module B"},
                ],
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert payload.get("error")
        assert "output_schema" in payload["error"]

    def test_schema_forwarded_to_omp_prompt(self):
        """The coerced schema rides the omp forward payload and lands in
        the child prompt (DEAD DEPTH: no hermes-side validate/retry turn,
        no schema_valid result keys)."""
        from tools import omp_delegation

        captured = {}

        def fake_dispatch(parent_agent, args):
            captured.update(args)
            return json.dumps({"status": "dispatched", "engine": "omp"})

        with (
            patch("tools.delegate_tool._load_config", return_value={}),
            patch.object(omp_delegation, "dispatch_omp_delegation", fake_dispatch),
        ):
            out = delegate_task(
                goal="produce the address",
                context="base context",
                output_schema=ADDRESS_SCHEMA,
                parent_agent=_make_mock_parent(),
            )
        assert json.loads(out)["status"] == "dispatched"
        forwarded = captured.get("tasks") or []
        assert forwarded and forwarded[0].get("output_schema") == ADDRESS_SCHEMA
        prompt = omp_delegation._build_task_prompt(
            forwarded[0]["goal"], forwarded[0].get("context"), forwarded[0]["output_schema"]
        )
        assert '"city"' in prompt
