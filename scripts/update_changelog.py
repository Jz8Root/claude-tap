#!/usr/bin/env python3
"""Insert a release section in CHANGELOG.md when one is missing."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
UNRELEASED_RE = re.compile(r"^## \[Unreleased\]\s*$", re.MULTILINE)
RELEASE_HEADING_RE = re.compile(r"^## \[(?P<version>\d+\.\d+\.\d+)\](?:\s+-\s+\d{4}-\d{2}-\d{2})?\s*$", re.MULTILINE)


def changelog_has_version(text: str, version: str) -> bool:
    return any(match.group("version") == version for match in RELEASE_HEADING_RE.finditer(text))


def unreleased_notes(changelog: str) -> str:
    match = UNRELEASED_RE.search(changelog)
    if not match:
        raise ValueError("Changelog is missing '## [Unreleased]' section")
    following = RELEASE_HEADING_RE.search(changelog, match.end())
    return changelog[match.end() : following.start() if following else len(changelog)].strip()


def render_release_section(version: str, release_date: dt.date, notes: str) -> str:
    return f"## [{version}] - {release_date.isoformat()}\n\n{notes.strip()}\n\n"


def insert_release_section(changelog: str, section: str) -> str:
    match = UNRELEASED_RE.search(changelog)
    if not match:
        raise ValueError("Changelog is missing '## [Unreleased]' section")
    following = RELEASE_HEADING_RE.search(changelog, match.end())
    history = changelog[following.start() :] if following else ""
    return changelog[: match.end()].rstrip() + "\n\n" + section.rstrip() + "\n\n" + history


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="Repository root path")
    parser.add_argument("--version", required=True, help="Release version without leading 'v', e.g. 0.1.40")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="Release date in YYYY-MM-DD format")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not VERSION_RE.match(args.version):
        raise SystemExit(f"Invalid version: {args.version}")
    release_date = dt.date.fromisoformat(args.date)
    repo_root = args.repo_root.resolve()
    paths = [repo_root / "CHANGELOG.md", repo_root / "CHANGELOG.zh.md"]
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"Required changelog is missing: {path.name}")
    documents = [(path, path.read_text(encoding="utf-8")) for path in paths]
    pending = [(path, text) for path, text in documents if not changelog_has_version(text, args.version)]
    if not pending:
        print(f"Changelogs already have [{args.version}]")
        return 0
    if len(pending) != len(documents):
        raise SystemExit("Release version must be present in both changelogs or neither")
    notes = [unreleased_notes(text) for _, text in pending]
    if any(notes) and not all(notes):
        raise SystemExit("Add matching Unreleased notes to both changelogs before releasing")
    template_root = Path(__file__).resolve().parent.parent / ".agents" / "templates"
    updates = []
    for (path, text), body, suffix in zip(pending, notes, ["", ".zh"]):
        if not body:
            body = (template_root / f"maintenance-release{suffix}.md").read_text(encoding="utf-8")
        section = render_release_section(args.version, release_date, body)
        updates.append((path, insert_release_section(text, section)))
    for path, updated in updates:
        path.write_text(updated, encoding="utf-8")
        print(f"Promoted {path.name} Unreleased notes to [{args.version}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
