# -*- coding: utf-8 -*-
"""Generate a privacy-preserving GitHub star history chart.

The script requests only aggregate repository metadata. It never requests or
writes stargazer usernames, user ids, avatars, or the authentication token.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_VERSION = "2026-03-10"
DEFAULT_JSON = Path("docs/data/star-history.json")
DEFAULT_SVG = Path("docs/assets/star-history.svg")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class StarStatsError(RuntimeError):
    """Raised when GitHub data is incomplete or cannot be validated."""


def _request_json(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "auto-okx-star-stats",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise StarStatsError(
            f"GitHub API request failed with HTTP {exc.code}; "
            "verify repository Metadata read access"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise StarStatsError(f"GitHub API request failed: {type(exc).__name__}") from exc


def fetch_star_total(repository: str, token: str) -> int:
    """Return the current aggregate star total from repository metadata."""
    if not REPOSITORY_RE.fullmatch(repository):
        raise StarStatsError("repository must use owner/name form")

    metadata = _request_json(f"https://api.github.com/repos/{repository}", token)
    try:
        total = int(metadata["stargazers_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StarStatsError("repository metadata has no valid stargazers_count") from exc
    if total < 0:
        raise StarStatsError("repository metadata has a negative stargazers_count")
    return total


def load_history(json_path: Path, repository: str) -> list[dict[str, Any]]:
    """Load and validate an existing aggregate history file."""
    if not json_path.exists():
        return []
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StarStatsError("existing star history is not valid JSON") from exc
    if payload.get("repository") != repository:
        raise StarStatsError("existing star history belongs to another repository")
    rows = payload.get("history")
    if not isinstance(rows, list):
        raise StarStatsError("existing star history is not a list")

    history: list[dict[str, Any]] = []
    previous = ""
    for row in rows:
        if not isinstance(row, dict):
            raise StarStatsError("existing star history contains a non-object row")
        day = row.get("date")
        stars = row.get("stars")
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise StarStatsError("existing star history contains an invalid date")
        if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
            raise StarStatsError("existing star history contains an invalid total")
        if previous and day <= previous:
            raise StarStatsError("existing star history dates are not strictly ordered")
        history.append({"date": day, "stars": stars})
        previous = day
    return history


def update_daily_history(
    history: list[dict[str, Any]], checked_on: str, total: int
) -> list[dict[str, Any]]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked_on):
        raise StarStatsError("checked_on must use YYYY-MM-DD")
    point = {"date": checked_on, "stars": total}
    if not history:
        return [point]
    if checked_on < str(history[-1]["date"]):
        raise StarStatsError("checked_on predates the latest aggregate point")
    if checked_on == history[-1]["date"]:
        return [*history[:-1], point]
    return [*history, point]


def _points(history: list[dict[str, Any]]) -> tuple[list[tuple[float, float]], int]:
    left, top, width, height = 78.0, 66.0, 824.0, 220.0
    maximum = max(4, max(int(row["stars"]) for row in history))
    if len(history) == 1:
        y = top + height - height * int(history[0]["stars"]) / maximum
        return [(left, y), (left + width, y)], maximum
    span = max(1, len(history) - 1)
    points = []
    for index, row in enumerate(history):
        x = left + width * index / span
        y = top + height - height * int(row["stars"]) / maximum
        points.append((x, y))
    return points, maximum


def render_svg(
    repository: str,
    history: list[dict[str, Any]],
    total: int,
    checked_on: str,
) -> str:
    points, maximum = _points(history)
    point_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    repo_text = html.escape(repository)
    first_day = html.escape(str(history[0]["date"]))
    last_day = html.escape(str(history[-1]["date"]))

    grid = []
    for index in range(5):
        y = 66 + 220 * index / 4
        value = round(maximum * (4 - index) / 4)
        grid.append(
            f'<line x1="78" y1="{y:.1f}" x2="902" y2="{y:.1f}" '
            'class="grid"/>'
            f'<text x="66" y="{y + 4:.1f}" text-anchor="end" '
            f'class="axis">{value}</text>'
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="360" viewBox="0 0 960 360" role="img" aria-labelledby="title desc">
  <title id="title">GitHub star history for {repo_text}</title>
  <desc id="desc">{total} stars recorded through {checked_on}</desc>
  <defs>
    <linearGradient id="area" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="#2f81f7" stop-opacity="0.40"/>
      <stop offset="100%" stop-color="#2f81f7" stop-opacity="0.03"/>
    </linearGradient>
    <style>
      .bg {{ fill: #0d1117; }}
      .grid {{ stroke: #30363d; stroke-width: 1; }}
      .axis {{ fill: #8b949e; font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      .title {{ fill: #f0f6fc; font: 600 20px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      .metric {{ fill: #f0f6fc; font: 700 28px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      .subtle {{ fill: #8b949e; font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      .line {{ fill: none; stroke: #58a6ff; stroke-width: 3; stroke-linejoin: round; stroke-linecap: round; }}
      .area {{ fill: url(#area); }}
    </style>
  </defs>
  <rect class="bg" width="960" height="360" rx="12"/>
  <text x="36" y="38" class="title">{repo_text} · Stars</text>
  <text x="924" y="40" text-anchor="end" class="metric">{total}</text>
  <text x="924" y="58" text-anchor="end" class="subtle">checked {checked_on} UTC</text>
  {"".join(grid)}
  <polygon class="area" points="78,286 {point_text} 902,286"/>
  <polyline class="line" points="{point_text}"/>
  <circle cx="{points[-1][0]:.1f}" cy="{points[-1][1]:.1f}" r="4.5" fill="#58a6ff"/>
  <text x="78" y="313" class="axis">{first_day}</text>
  <text x="902" y="313" text-anchor="end" class="axis">{last_day}</text>
  <text x="480" y="340" text-anchor="middle" class="subtle">Daily cumulative GitHub stars · no user identities stored</text>
</svg>
"""


def write_outputs(
    repository: str,
    total: int,
    checked_on: str,
    json_path: Path,
    svg_path: Path,
) -> None:
    history = update_daily_history(
        load_history(json_path, repository), checked_on, total
    )

    payload = {
        "repository": repository,
        "checked_on": checked_on,
        "total_stars": total,
        "history": history,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    svg_path.write_text(
        render_svg(repository, history, total, checked_on),
        encoding="utf-8",
        newline="\n",
    )


def self_test() -> None:
    history = update_daily_history([], "2026-01-01", 2)
    history = update_daily_history(history, "2026-01-03", 3)
    history = update_daily_history(history, "2026-01-04", 3)
    assert history == [
        {"date": "2026-01-01", "stars": 2},
        {"date": "2026-01-03", "stars": 3},
        {"date": "2026-01-04", "stars": 3},
    ]
    history = update_daily_history(history, "2026-01-04", 4)
    assert history[-1] == {"date": "2026-01-04", "stars": 4}
    svg = render_svg("owner/repo", history, 4, "2026-01-04")
    assert "<script" not in svg.lower()
    assert "owner/repo" in svg
    print("star stats self-test passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate aggregate GitHub star history JSON and SVG"
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get(
            "GITHUB_REPOSITORY", "asd976385560/AUTO-OKX-USDT-M"
        ),
        help="GitHub repository in owner/name form",
    )
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--svg-output", type=Path, default=DEFAULT_SVG)
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="environment variable containing a repository-scoped token",
    )
    parser.add_argument(
        "--checked-on",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="UTC date used for the final chart point",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0
    token = os.environ.get(args.token_env, "")
    try:
        total = fetch_star_total(args.repo, token)
        write_outputs(
            args.repo,
            total,
            args.checked_on,
            args.json_output,
            args.svg_output,
        )
    except StarStatsError as exc:
        print(f"[star-stats] {exc}", file=sys.stderr)
        return 1
    print(
        f"[star-stats] repository={args.repo} total={total} "
        f"checked_on={args.checked_on}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
