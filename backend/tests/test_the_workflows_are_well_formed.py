"""The CI contract should fail locally before a broken workflow is published."""

from pathlib import Path
import re

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse GitHub Actions YAML")

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT_PATH = re.compile(r"\b(?:python|python3|node)\s+(scripts/[\w./-]+|native/tests/[\w./-]+)")
ACTION_PIN = re.compile(r"^[^\s@]+@(?:v\d+(?:\.\d+)*|[0-9a-f]{40})$")
REUSABLE_MAIN = {
    "augbastos/.github/.github/workflows/scpe-verify.yml@main",
    "augbastos/.github/.github/workflows/scpe-seal-reusable.yml@main",
}
EXISTING_FLOATING_PIN = "dtolnay/rust-toolchain@stable"


def check_workflow(document, name):
    assert isinstance(document, dict), f"{name}: expected a YAML mapping"
    assert "permissions" in document, f"{name}: missing top-level permissions"
    assert "on" in document, f"{name}: missing event trigger"
    for job_name, job in document["jobs"].items():
        # Reusable-workflow caller jobs cannot declare runs-on in Actions YAML.
        assert "runs-on" in job or "uses" in job, f"{name}/{job_name}: no runner or reusable workflow"
        for step in [job, *job.get("steps", [])]:
            action = step.get("uses")
            if action:
                assert ACTION_PIN.fullmatch(action) or action in REUSABLE_MAIN or action == EXISTING_FLOATING_PIN, (
                    f"{name}/{job_name}: unpinned action {action}")
            for path in SCRIPT_PATH.findall(step.get("run", "")):
                assert (ROOT / path).is_file(), f"{name}/{job_name}: missing {path}"


def test_the_workflows_are_well_formed():
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert paths, "no workflows found"
    for path in paths:
        # BaseLoader keeps YAML 1.1 from converting GitHub's `on` key to True.
        check_workflow(yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader), path.name)


def test_a_malformed_workflow_makes_the_check_fail():
    valid = {"on": {"pull_request": ""}, "permissions": {"contents": "read"},
             "jobs": {"check": {"runs-on": "ubuntu-latest", "steps": [
                 {"uses": "actions/checkout@v4"}]}}}
    check_workflow(valid, "control.yml")
    del valid["jobs"]["check"]["runs-on"]
    with pytest.raises(AssertionError, match="no runner"):
        check_workflow(valid, "control.yml")
    valid["jobs"]["check"]["runs-on"] = "ubuntu-latest"
    valid["jobs"]["check"]["steps"][0]["uses"] = "actions/checkout@main"
    with pytest.raises(AssertionError, match="unpinned action"):
        check_workflow(valid, "control.yml")
    valid["jobs"]["check"]["steps"][0] = {"run": "python scripts/missing-ci-control.py"}
    with pytest.raises(AssertionError, match="missing scripts/missing-ci-control.py"):
        check_workflow(valid, "control.yml")
