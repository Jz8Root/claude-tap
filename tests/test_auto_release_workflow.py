"""Regression tests for the auto-release workflow shell contract."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "auto-release.yml"


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_release_pr_body_satisfies_policy_sections() -> None:
    workflow = _workflow_text()

    assert "## Summary" in workflow
    assert "## Validation" in workflow
    assert "## Evidence" in workflow
    assert '--body-file "$pr_body_file"' in workflow
    assert "Auto-generated release changelog" not in workflow


def test_existing_release_pr_body_is_fixed_before_ci_triggering_push() -> None:
    workflow = _workflow_text()

    assert workflow.index('gh pr edit "$pr_number" --body-file "$pr_body_file"') < workflow.index(
        'git push --force-with-lease origin "$branch"'
    )


def test_release_pr_waits_for_checks_before_merge() -> None:
    workflow = _workflow_text()

    assert workflow.index(
        'pr_head="$(gh pr view "$pr_number" --json headRefOid --jq \'.headRefOid\')"'
    ) < workflow.index('gh pr checks "$pr_number" --watch --fail-fast --interval 10')
    assert workflow.index('gh pr checks "$pr_number" --watch --fail-fast --interval 10') < workflow.index(
        'gh pr merge "$pr_number" --match-head-commit "$release_commit" --squash --delete-branch'
    )


@pytest.mark.parametrize("state", ["BLOCKED", "UNKNOWN", "CLEAN"])
def test_release_merge_guard_waits_for_review_without_bypassing_rules(tmp_path, state: str) -> None:
    workflow = _workflow_text()
    start = workflow.index('          echo "changed=true"')
    end = workflow.index("\n      - name: Create and push tag", start)
    script = textwrap.dedent(workflow[start:end])
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        "    print(os.environ['TEST_MERGE_STATE'])\n"
        "elif sys.argv[1:3] == ['pr', 'merge']:\n"
        "    Path(os.environ['TEST_MERGE_ARGS']).write_text(json.dumps(sys.argv[1:]))\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    merge_args = tmp_path / "merge-args"
    result = subprocess.run(
        ["bash", "-eu", "-c", "pr_number=442; release_commit=reviewed-sha;\n" + script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "TEST_MERGE_STATE": state,
            "TEST_MERGE_ARGS": str(merge_args),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "changed=true\n"
    args = json.loads(merge_args.read_text())
    expected = ["pr", "merge", "442"]
    if state != "CLEAN":
        expected.append("--auto")
    expected.extend(["--match-head-commit", "reviewed-sha", "--squash", "--delete-branch"])
    assert args == expected
    if state == "CLEAN":
        assert not summary.exists()
    else:
        assert "Auto-merge enabled" in summary.read_text()
        assert "awaits maintainer review" in summary.read_text()
        assert "::notice::" in result.stdout
