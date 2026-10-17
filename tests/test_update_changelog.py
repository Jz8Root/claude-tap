"""Exercise release-note promotion through the real filesystem CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "update_changelog.py"


def run_generator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(root), "--version", "0.1.146", "--date", "2026-10-17"],
        capture_output=True,
        text=True,
    )


def test_promotes_curated_bilingual_notes_once_without_duplicating_history(tmp_path) -> None:
    notes = ["### Fixed\n\n- Preserve stdout.\n", "### Fixed\n\n- Translated stdout fix.\n"]
    paths = [tmp_path / "CHANGELOG.md", tmp_path / "CHANGELOG.zh.md"]
    histories = []
    for path, body in zip(paths, notes):
        history = "## [0.1.145] - 2026-08-16\n\n- Previous release.\n"
        histories.append(history)
        path.write_text("# Changelog\n\n## [Unreleased]\n\n" + body + "\n" + history)
    result = run_generator(tmp_path)
    assert result.returncode == 0, result.stderr
    snapshots = []
    for path, body, history in zip(paths, notes, histories):
        updated = path.read_text()
        assert updated.startswith("# Changelog\n\n## [Unreleased]\n\n## [0.1.146] - 2026-10-17\n")
        assert updated.count(body.strip()) == 1
        assert updated.endswith(history)
        snapshots.append(updated)
    assert run_generator(tmp_path).returncode == 0
    assert [path.read_text() for path in paths] == snapshots


def test_empty_notes_produce_maintenance_release_without_git_subjects(tmp_path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## [Unreleased]\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "chore(evidence): retake screenshots",
        ],
        check=True,
    )
    assert run_generator(tmp_path).returncode == 0
    assert "Maintenance release." in path.read_text()
    assert "retake screenshots" not in path.read_text()


@pytest.mark.parametrize("failure", ["missing_heading", "untranslated_notes", "partial_version"])
def test_invalid_bilingual_inputs_do_not_partially_write_files(tmp_path, failure: str) -> None:
    english = "# Changelog\n\n## [Unreleased]\n\n- Public fix.\n"
    chinese = english
    if failure == "missing_heading":
        chinese = "# Changelog\n"
    elif failure == "untranslated_notes":
        chinese = "# Changelog\n\n## [Unreleased]\n"
    else:
        english += "\n## [0.1.146] - 2026-10-17\n\n- Released.\n"
    paths = [tmp_path / "CHANGELOG.md", tmp_path / "CHANGELOG.zh.md"]
    for path, text in zip(paths, [english, chinese]):
        path.write_text(text)
    result = run_generator(tmp_path)
    assert result.returncode != 0
    assert [path.read_text() for path in paths] == [english, chinese]
