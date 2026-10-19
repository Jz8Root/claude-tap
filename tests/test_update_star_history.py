"""Tests for the GitHub-native star-history chart updater."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.check_screenshots import analyze_file
from scripts.update_star_history import fetch_stargazer_timestamps, render_charts


def test_fetch_stargazer_timestamps_paginates_and_authenticates():
    requests = []
    latest_week = datetime(2026, 8, 2, tzinfo=UTC)
    first_page = [
        {"week": int((latest_week - timedelta(weeks=n)).timestamp()), "total": 0, "days": [0] * 7} for n in range(30)
    ]
    first_page[0].update(total=3, days=[0, 0, 2, 0, 0, 0, 1])
    older_week = latest_week - timedelta(weeks=30)
    second_page = [{"week": int(older_week.timestamp()), "total": 1, "days": [1, 0, 0, 0, 0, 0, 0]}]

    def request_json(request):
        requests.append(request)
        page = parse_qs(urlsplit(request.full_url).query)["page"][0]
        return first_page if page == "1" else second_page

    timestamps = fetch_stargazer_timestamps("liaohch3/claude-tap", "test-token", request_json)

    assert timestamps == [
        older_week,
        latest_week + timedelta(days=2),
        latest_week + timedelta(days=2),
        latest_week + timedelta(days=6),
    ]
    assert len(requests) == 2
    for page, request in enumerate(requests, start=1):
        assert (
            request.full_url
            == f"https://api.github.com/repos/liaohch3/claude-tap/stargazers/history?per_page=30&page={page}"
        )
        assert request.method == "GET"
        assert request.data is None
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-github-api-version"] == "2026-03-10"


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"message": "Resource not accessible by integration"}, "not a list"),
        ([{"days": [0] * 7, "total": 0}], "week timestamp"),
        ([{"week": 0, "days": [1] * 6, "total": 6}], "daily counts"),
        ([{"week": 0, "days": [-1] + [0] * 6, "total": -1}], "daily counts"),
        ([{"week": 0, "days": [True] + [0] * 6, "total": 1}], "daily counts"),
        ([{"week": 0, "days": [1] + [0] * 6, "total": 2}], "daily counts"),
        ([{"week": 0, "days": [0] * 7, "total": 0}] * 2, "repeated a week"),
        ([], "no star history"),
    ],
)
def test_fetch_stargazer_timestamps_rejects_malformed_payload(payload, message):
    with pytest.raises(RuntimeError, match=message):
        fetch_stargazer_timestamps("liaohch3/claude-tap", "test-token", lambda _request: payload)


@pytest.mark.parametrize(
    "body, expected",
    [
        (b'{"message":"Resource not accessible by integration"}', "Resource not accessible by integration"),
        (b'{"message":"Authorization test-token\\nwas rejected"}', "Authorization [REDACTED] was rejected"),
        (b"not JSON", "Forbidden"),
    ],
)
def test_http_errors_report_the_cause_without_exposing_the_token(body, expected):
    def request_json(request):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, BytesIO(body))

    with pytest.raises(RuntimeError) as caught:
        fetch_stargazer_timestamps("liaohch3/claude-tap", "test-token", request_json)
    message = str(caught.value)
    assert "HTTP 403" in message
    assert expected in message
    assert "test-token" not in message
    assert "\n" not in message


@pytest.mark.parametrize("repo", ["claude-tap", "owner/repo/extra", "../repo"])
def test_fetch_stargazer_timestamps_rejects_invalid_repo(repo):
    with pytest.raises(ValueError, match="OWNER/REPO"):
        fetch_stargazer_timestamps(repo, token="test-token", request_json=lambda _request: {})


def test_fetch_stargazer_timestamps_requires_token():
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN is required"):
        fetch_stargazer_timestamps("liaohch3/claude-tap", token=None)


def test_render_charts_persists_valid_light_and_dark_pngs(tmp_path, monkeypatch):
    def reject_duplicate_title(*_args, **_kwargs):
        raise AssertionError("the README section already provides the chart title")

    monkeypatch.setattr("matplotlib.axes.Axes.set_title", reject_duplicate_title)
    timestamps = [
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 6, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    ]

    render_charts("liaohch3/claude-tap", timestamps, tmp_path)

    light = tmp_path / "star-history-light.png"
    dark = tmp_path / "star-history-dark.png"
    for chart in (light, dark):
        result = analyze_file(chart)
        assert result.status == "PASS", result.failures + result.warnings
        assert result.info is not None
        assert (result.info.width, result.info.height) == (1600, 900)
        assert chart.stat().st_size > 10_000
    assert light.read_bytes() != dark.read_bytes()


def test_star_history_workflow_publishes_to_asset_branch():
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "star-history.yml").read_text()

    assert 'cron: "0 19 * * *"' in workflow
    assert workflow.count("contents: read") == 1
    assert workflow.count("contents: write") == 1
    assert "needs: generate" in workflow
    assert "ref: star-history-assets" in workflow
    assert "scripts/update_star_history.py" in workflow
    assert "scripts/check_screenshots.py" in workflow
    assert "matplotlib==3.11.1" in workflow
    assert "secrets.RELEASE_BOT_TOKEN" not in workflow
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert workflow.index("contents: read") < workflow.index("matplotlib==3.11.1")
    assert workflow.index("contents: write") > workflow.index("matplotlib==3.11.1")
    assert "timeout-minutes: 10" in workflow
    assert "git diff --cached --quiet" in workflow
    assert workflow.index("git add star-history-light.png") < workflow.index("git diff --cached --quiet")
    assert "git push" in workflow
