#!/usr/bin/env python3
"""Build MCP publish/config-template notifications from the tracking issue's
audience matrix (issue #25 — config only, never posted to).

Writes a title+body file pair per --output-dir group:
  - one "upgrade available" issue per distinct audience among published servers
  - one "config template updated" issue per distinct audience among servers
    whose config_templates entry changed but weren't published this run
Creating a standalone GitHub issue from each pair (labeled mcp-notification,
left open) is the caller's job (see publish.yml) so a team is only
@mentioned for the MCPs that concern them, and no shared thread ever
accumulates cross-team subscriptions.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_matrix(issue_body: str) -> dict[str, list[str]]:
    """Parse the ```yaml service: team, team ... ``` block from the issue body."""
    match = re.search(r"```ya?ml\n(.*?)```", issue_body, re.DOTALL)
    if not match:
        return {}
    matrix: dict[str, list[str]] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        server, _, teams = line.partition(":")
        matrix[server.strip()] = [team.strip() for team in teams.split(",") if team.strip()]
    return matrix


def group_by_audience(servers: list[str], matrix: dict[str, list[str]]) -> list[tuple[tuple[str, ...], list[str]]]:
    """Group servers by their exact, order-independent set of notified teams."""
    groups: dict[tuple[str, ...], list[str]] = {}
    for server in sorted(servers):
        key = tuple(sorted(matrix.get(server, [])))
        groups.setdefault(key, []).append(server)
    return list(groups.items())


def mention_line(teams: tuple[str, ...], gh_org: str) -> str:
    if not teams:
        return "_no audience configured — add these MCPs to the matrix in this issue's description._"
    return " ".join(f"@{gh_org}/{team}" for team in teams)


def unique_files(servers: list[str], template_changes: dict[str, list[str]]) -> list[str]:
    seen: list[str] = []
    for server in servers:
        for file_path in template_changes.get(server, []):
            if file_path not in seen:
                seen.append(file_path)
    return seen


def file_links(file_paths: list[str], gh_org: str, repo_name: str) -> list[str]:
    return [
        f"- [{path}](https://github.com/{gh_org}/{repo_name}/blob/main/{path})"
        for path in file_paths
    ]


def build_upgrade_post(
    servers: list[str],
    teams: tuple[str, ...],
    template_changes: dict[str, list[str]],
    gh_org: str,
    ghcr_owner: str,
    repo_name: str,
    pr_ref: str,
) -> tuple[str, str]:
    names_list = ", ".join(servers)
    names = ", ".join(f"**{s}**" for s in servers)
    title = f"🆙 MCP upgrade: {names_list}"

    via = f" via {pr_ref}" if pr_ref else ""
    lines = [f"The following MCPs were updated{via}: {names}", ""]
    lines.append(
        "You can upgrade it by running the following command:"
        if len(servers) == 1
        else "You can upgrade them by running the following commands:"
    )
    lines.append("")
    lines.append("```")
    lines += [f"docker pull ghcr.io/{ghcr_owner}/{repo_name}/lvx-{s}:latest" for s in servers]
    lines.append("```")

    templated = [s for s in servers if s in template_changes]
    if templated:
        templ_names = ", ".join(f"**{s}**" for s in templated)
        lines += ["", f"Config template also updated for {templ_names} — see:"]
        lines += file_links(unique_files(templated, template_changes), gh_org, repo_name)

    lines += ["", mention_line(teams, gh_org)]
    lines += ["", "See https://github.com/" + gh_org + "/" + repo_name
              + "/blob/main/README.md#step-1--pull-the-images for the full setup steps."]
    lines += ["", "_🤖 Automated notification from the MCP publish pipeline — not created manually. No action is required on this issue._"]
    return title, "\n".join(lines)


def build_template_only_post(
    servers: list[str],
    teams: tuple[str, ...],
    template_changes: dict[str, list[str]],
    gh_org: str,
    repo_name: str,
    pr_ref: str,
) -> tuple[str, str]:
    names_list = ", ".join(servers)
    names = ", ".join(f"**{s}**" for s in servers)
    label = "template" if len(servers) == 1 else "templates"
    title = f"⚙️ MCP config template updated: {names_list}"

    via = f" via {pr_ref}" if pr_ref else ""
    lines = [f"The config {label} for {names} changed{via} (no new image this time):"]
    lines += file_links(unique_files(servers, template_changes), gh_org, repo_name)
    lines += ["", mention_line(teams, gh_org)]
    lines += ["", "_🤖 Automated notification from the MCP publish pipeline — not created manually. No action is required on this issue._"]
    return title, "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-body-file", required=True)
    parser.add_argument("--published", required=True, help='JSON array of image names, e.g. ["lvx-jira"]')
    parser.add_argument("--template-changes", required=True, help='JSON object, e.g. {"jira": ["config_templates/..."]}')
    parser.add_argument("--gh-org", required=True, help="GitHub org login, for @mentions and file links (e.g. johalduran7)")
    parser.add_argument("--ghcr-owner", required=True, help="GHCR namespace, lowercase (e.g. johalduran7)")
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--pr-ref", default="", help='PR reference to credit, e.g. "#123" (empty if not applicable)')
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    with open(args.issue_body_file, encoding="utf-8") as f:
        matrix = parse_matrix(f.read())

    published_images: list[str] = json.loads(args.published)
    published = sorted({image.removeprefix("lvx-") for image in published_images})
    template_changes: dict[str, list[str]] = json.loads(args.template_changes)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    group_num = 0

    def write_group(title: str, body: str) -> None:
        nonlocal group_num
        group_num += 1
        (output_dir / f"group-{group_num:02d}.title.txt").write_text(title, encoding="utf-8")
        (output_dir / f"group-{group_num:02d}.body.md").write_text(body, encoding="utf-8")

    for teams, servers in group_by_audience(published, matrix):
        write_group(*build_upgrade_post(servers, teams, template_changes, args.gh_org, args.ghcr_owner, args.repo_name, args.pr_ref))

    template_only = sorted(set(template_changes) - set(published))
    for teams, servers in group_by_audience(template_only, matrix):
        write_group(*build_template_only_post(servers, teams, template_changes, args.gh_org, args.repo_name, args.pr_ref))

    print(f"Wrote {group_num} issue(s) to {output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
