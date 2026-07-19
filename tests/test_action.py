"""The composite GitHub Action and the example workflow.

`action.yml` is schema-validated rather than merely loaded, because a typo in it
only shows up when someone else's workflow run fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = ROOT / "action.yml"
WORKFLOW_PATH = ROOT / "examples" / "workflows" / "refresh-map-data.yml"

#: Schema for the subset of the GitHub Actions metadata syntax this action uses.
ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "description", "runs"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "author": {"type": "string"},
        "description": {"type": "string", "minLength": 1},
        "branding": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"icon": {"type": "string"}, "color": {"type": "string"}},
        },
        "inputs": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["description"],
                "additionalProperties": False,
                "properties": {
                    "description": {"type": "string", "minLength": 1},
                    "required": {"type": "boolean"},
                    "default": {"type": ["string", "boolean", "number"]},
                    "deprecationMessage": {"type": "string"},
                },
            },
        },
        "outputs": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["description", "value"],
                "additionalProperties": False,
                "properties": {
                    "description": {"type": "string", "minLength": 1},
                    "value": {"type": "string", "minLength": 1},
                },
            },
        },
        "runs": {
            "type": "object",
            "required": ["using", "steps"],
            "additionalProperties": False,
            "properties": {
                "using": {"const": "composite"},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "id": {"type": "string"},
                            "if": {"type": "string"},
                            "uses": {"type": "string"},
                            "run": {"type": "string"},
                            "shell": {"type": "string"},
                            "with": {"type": "object"},
                            "env": {"type": "object"},
                            "working-directory": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


@pytest.fixture(scope="module")
def action() -> dict[str, Any]:
    return yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_action_matches_the_metadata_schema(action: dict[str, Any]) -> None:
    jsonschema.validate(action, ACTION_SCHEMA)


def test_action_declares_the_documented_inputs(action: dict[str, Any]) -> None:
    assert set(action["inputs"]) >= {"config", "python-version", "args"}
    assert action["inputs"]["config"]["required"] is True


def test_action_declares_the_documented_outputs(action: dict[str, Any]) -> None:
    assert set(action["outputs"]) >= {"changed", "manifest-path", "stale"}
    for output in action["outputs"].values():
        assert output["value"].startswith("${{ steps.refresh.outputs.")


def test_every_run_step_declares_a_shell(action: dict[str, Any]) -> None:
    """Composite actions fail at load time if a `run` step has no `shell`."""
    for step in action["runs"]["steps"]:
        if "run" in step:
            assert step.get("shell"), f"step {step.get('name')!r} has no shell"


def test_steps_are_either_run_or_uses(action: dict[str, Any]) -> None:
    for step in action["runs"]["steps"]:
        assert ("run" in step) != ("uses" in step)


def test_the_output_producing_step_has_the_referenced_id(action: dict[str, Any]) -> None:
    ids = {step.get("id") for step in action["runs"]["steps"]}
    assert "refresh" in ids


def test_action_treats_exit_code_three_as_success(action: dict[str, Any]) -> None:
    script = next(
        step["run"] for step in action["runs"]["steps"] if step.get("id") == "refresh"
    )
    assert '"$code" -ne 3' in script
    assert "--summary" in script and "GITHUB_OUTPUT" in script


def test_action_puts_the_package_on_the_python_path(action: dict[str, Any]) -> None:
    step = next(step for step in action["runs"]["steps"] if step.get("id") == "refresh")
    assert step["env"]["PYTHONPATH"].endswith("/src")


def test_action_installs_the_pinned_requirements(action: dict[str, Any]) -> None:
    scripts = " ".join(step.get("run", "") for step in action["runs"]["steps"])
    assert "requirements.txt" in scripts
    assert "SQLAlchemy" in scripts


def test_example_workflow_is_valid_yaml_and_uses_the_action(workflow: dict[str, Any]) -> None:
    # PyYAML parses the `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "schedule" in triggers
    assert "repository_dispatch" in triggers
    steps = workflow["jobs"]["refresh"]["steps"]
    uses = [step.get("uses", "") for step in steps]
    assert any("geo-refresh-pipeline" in item for item in uses)


def test_example_workflow_gates_publishing_on_the_changed_output(
    workflow: dict[str, Any],
) -> None:
    steps = workflow["jobs"]["refresh"]["steps"]
    guarded = [step for step in steps if "if" in step]
    assert guarded, "the example must guard its publish steps"
    for step in guarded:
        assert "steps.refresh.outputs.changed == 'true'" in step["if"]
    assert workflow["jobs"]["deploy"]["if"] == "needs.refresh.outputs.changed == 'true'"


def test_example_workflow_only_uses_declared_action_inputs(
    workflow: dict[str, Any], action: dict[str, Any]
) -> None:
    for step in workflow["jobs"]["refresh"]["steps"]:
        if "geo-refresh-pipeline" in step.get("uses", ""):
            assert set(step.get("with", {})) <= set(action["inputs"])


def test_example_workflow_only_reads_declared_action_outputs(
    workflow: dict[str, Any], action: dict[str, Any]
) -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    referenced = {
        line.split("steps.refresh.outputs.")[1].split("}")[0].split()[0].strip()
        for line in text.splitlines()
        if "steps.refresh.outputs." in line
    }
    assert referenced <= set(action["outputs"]), referenced - set(action["outputs"])


def test_ci_workflow_runs_the_suite() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    scripts = " ".join(
        step.get("run", "")
        for job in ci["jobs"].values()
        for step in job["steps"]
    )
    assert "pytest" in scripts
    assert "requirements.txt" in scripts
