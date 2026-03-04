#!/usr/bin/env python3
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_REPOSITORY = "Arteco-Global/uss-launcher"
API_BASE = "https://api.github.com/repos/{owner}/{repo}/releases"
RELEASE_BASE = "https://api.github.com/repos/{owner}/{repo}/releases/{release_id}"
OUTPUT_PATH = Path("docs/release-downloads.md")
STATS_START = "<!-- download-stats:start -->"
STATS_END = "<!-- download-stats:end -->"


def escape_markdown(text: str) -> str:
    return text.replace("|", "\\|")


def parse_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_repository() -> tuple[str, str]:
    repo = os.getenv("GITHUB_REPOSITORY", DEFAULT_REPOSITORY)
    if "/" not in repo:
        raise ValueError(f"Invalid repository value: {repo!r}")
    owner, name = repo.split("/", 1)
    return owner, name


def build_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "release-downloads-script",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    request_headers = dict(headers)
    body = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as err:
        details = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API HTTP {err.code}: {details}") from err
    except URLError as err:
        raise RuntimeError(f"GitHub API connection error: {err.reason}") from err

    return json.loads(payload) if payload else {}


def fetch_releases(owner: str, repo: str, headers: dict[str, str]) -> list[dict]:
    releases: list[dict] = []
    page = 1
    while True:
        query = urlencode({"per_page": 100, "page": page})
        url = API_BASE.format(owner=owner, repo=repo) + f"?{query}"
        page_data = request_json(url, headers)
        if not isinstance(page_data, list):
            raise RuntimeError(f"Unexpected GitHub API response: {page_data}")
        if not page_data:
            break
        releases.extend(page_data)
        page += 1
    return releases


def collect_asset_stats(assets: list[dict]) -> tuple[int, list[dict[str, Any]]]:
    total = 0
    asset_items: list[dict[str, Any]] = []
    for asset in assets:
        downloads = int(asset.get("download_count", 0) or 0)
        total += downloads
        asset_items.append(
            {
                "name": str(asset.get("name", "unknown")),
                "downloads": downloads,
            }
        )
    return total, asset_items


def format_assets_for_docs(asset_items: list[dict[str, Any]]) -> str:
    lines = []
    for item in asset_items:
        lines.append(f'{escape_markdown(str(item["name"]))}: {int(item["downloads"])}')
    return "<br>".join(lines) if lines else "-"


def render_markdown(rows: list[dict], repository: str) -> str:
    lines = [
        "# Release downloads\n\n",
        f"Repository: `{repository}`\n\n",
        "| Tag | Date | Total downloads | Assets |\n",
        "|---|---|---|---|\n",
    ]

    if not rows:
        lines.append("| - | - | 0 | No published releases found |\n")
        return "".join(lines)

    for row in rows:
        lines.append(
            f'| {row["tag"]} | {row["published"]} | {row["total"]} | {row["assets"]} |\n'
        )

    return "".join(lines)


def render_stats_block(total: int, asset_items: list[dict[str, Any]]) -> str:
    lines = [
        STATS_START + "\n",
        "## Download stats\n\n",
        f"Total downloads: **{total}**\n\n",
    ]
    if asset_items:
        lines.append("| Asset | Downloads |\n")
        lines.append("|---|---:|\n")
        for item in asset_items:
            lines.append(
                f'| {escape_markdown(str(item["name"]))} | {int(item["downloads"])} |\n'
            )
    else:
        lines.append("- No assets available for this release.\n")
    lines.append("\n" + STATS_END)
    return "".join(lines)


def upsert_stats_block(body: str, stats_block: str) -> str:
    stripped_body = body.strip()
    if STATS_START in body and STATS_END in body:
        start_index = body.index(STATS_START)
        end_index = body.index(STATS_END, start_index) + len(STATS_END)
        before = body[:start_index].strip()
        after = body[end_index:].strip()
        sections = [section for section in [before, stats_block, after] if section]
        return "\n\n".join(sections).strip() + "\n"

    if stripped_body:
        return f"{stripped_body}\n\n{stats_block}\n"
    return f"{stats_block}\n"


def update_release_body(
    owner: str, repo: str, release_id: int, body: str, headers: dict[str, str]
) -> None:
    url = RELEASE_BASE.format(owner=owner, repo=repo, release_id=release_id)
    request_json(url, headers, method="PATCH", payload={"body": body})


def sync_release_bodies(rows: list[dict], owner: str, repo: str, headers: dict[str, str]) -> int:
    updates = 0
    for row in rows:
        current_body = str(row["body"])
        stats_block = render_stats_block(int(row["total"]), list(row["asset_items"]))
        next_body = upsert_stats_block(current_body, stats_block)
        if next_body == current_body:
            continue
        update_release_body(owner, repo, int(row["id"]), next_body, headers)
        updates += 1
    return updates


def main() -> None:
    owner, repo = parse_repository()
    repository = f"{owner}/{repo}"
    headers = build_headers()
    update_release_bodies = parse_bool("UPDATE_RELEASE_BODIES", default=False)
    if update_release_bodies and "Authorization" not in headers:
        raise RuntimeError("UPDATE_RELEASE_BODIES=true requires GITHUB_TOKEN")

    releases = fetch_releases(owner, repo, headers)

    rows = []
    for release in releases:
        if release.get("draft"):
            continue

        assets = release.get("assets", [])
        total, asset_items = collect_asset_stats(assets)
        assets_rendered = format_assets_for_docs(asset_items)
        published_at = release.get("published_at") or release.get("created_at") or ""
        published = published_at[:10] if published_at else "-"
        tag = escape_markdown(str(release.get("tag_name", "unknown")))

        rows.append(
            {
                "id": int(release.get("id", 0) or 0),
                "tag": tag,
                "published": published,
                "published_at": published_at,
                "total": total,
                "assets": assets_rendered,
                "asset_items": asset_items,
                "body": str(release.get("body") or ""),
            }
        )

    rows.sort(key=lambda row: row["published_at"], reverse=True)

    if update_release_bodies:
        updated_count = sync_release_bodies(rows, owner, repo, headers)
        print(f"Release bodies updated: {updated_count}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render_markdown(rows, repository), encoding="utf-8")


if __name__ == "__main__":
    main()
