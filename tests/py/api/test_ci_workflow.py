"""Task 4: CI runs pytest and pip-audit; workflow is valid YAML."""

import pathlib
import yaml


WORKFLOW = pathlib.Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


class TestCiWorkflow:
    def test_workflow_exists_and_is_valid_yaml(self):
        assert WORKFLOW.exists(), "missing .github/workflows/ci.yml"
        data = yaml.safe_load(WORKFLOW.read_text())
        assert isinstance(data, dict)
        assert "jobs" in data

    def test_workflow_runs_pytest(self):
        text = WORKFLOW.read_text()
        assert "pytest" in text
        assert "uv run" in text

    def test_workflow_runs_pip_audit(self):
        text = WORKFLOW.read_text()
        assert "pip-audit" in text or "pip_audit" in text

    def test_pyproject_has_pip_audit_dev_dep(self):
        text = (pathlib.Path(__file__).resolve().parents[3] / "apps" / "api-py" / "pyproject.toml").read_text()
        assert "pip-audit" in text

    def test_workflow_runs_where_the_project_is(self):
        # The first version ran `uv run pytest` at the repo root, where there is no
        # pyproject.toml: every run failed in 5 s and the test above still passed.
        data = yaml.safe_load(WORKFLOW.read_text())
        job = data["jobs"]["ci"]
        assert job["defaults"]["run"]["working-directory"] == "apps/api-py"
        assert any("pytest" in step.get("run", "") and "../../tests" in step.get("run", "")
                   for step in job["steps"])

    def test_workflow_checks_out_the_schema_repo(self):
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["ci"]["steps"]
        assert any(step.get("with", {}).get("repository") == "nik129linux/cantrack-db"
                   and step["with"].get("path") == "cantrack-db" for step in steps)
