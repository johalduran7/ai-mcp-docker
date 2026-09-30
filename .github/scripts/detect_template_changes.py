#!/usr/bin/env python3
"""Detect which servers' config_templates entries changed between two refs.

Attributes a changed line to a server by first finding that server's actual
*block* in the file, then checking whether any changed line number falls
inside it — not by requiring the changed line itself to mention the service.
That distinction matters because edits often land on lines (e.g. `"command"`,
an `args` entry, an env var) that never contain the literal `lvx-<server>`
string, which only appears elsewhere in the same block (e.g. the docker image
reference, or a markdown code-fence's `claude mcp add-json lvx-<server>`
line). Lines outside every service's block (readme arrays, prose, headings)
are never attributed to any service, so doc-only edits raise no notification.

Two block shapes are supported, auto-detected per file:
  - JSON (claude_desktop_config*.json, mcp.json.*.example): a top-level
    `"servers"`/`"mcpServers"` object where each server is its own key --
    block boundaries are found via brace-depth tracking.
  - Markdown (claude_cli_mcp_add.*.md): sections delimited by `#`-headings,
    each containing exactly one server's `claude mcp add-json lvx-<server>`
    snippet -- block = heading line through the line before the next heading.

Falls back to whole-file substring matching (the old behavior) if a file
matches neither shape, so an unrecognized template still gets *something*
attributed rather than silently dropping the notification.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

CONTAINER_RE = re.compile(r'^\s*"(?:servers|mcpServers)"\s*:\s*\{')
KEY_RE = re.compile(r'^\s*"([A-Za-z0-9_-]+)"\s*:\s*\{')
HEADING_RE = re.compile(r"^#{1,6}\s")
# The one unambiguous anchor for "this section registers <service>" -- unlike a
# bare `lvx-<service>` substring, this can't false-match prose that mentions a
# *different* service by name (e.g. "opensearch shares the lvx-aws credential
# model"). `.*?` tolerates flags between the subcommand and the server name
# (e.g. the real `claude mcp add-json -s user lvx-argocd '{`), which the old
# exact-adjacency pattern never matched -- silently emptying every block and
# falling back to whole-file substring matching for every template edit.
ADD_JSON_RE = re.compile(r"claude mcp add-json\b.*?\blvx-([a-z0-9_-]+)")


def changed_files(before: str, after: str) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", before, after, "--", "config_templates"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def file_lines(ref: str, path: str) -> list[str] | None:
    try:
        raw = subprocess.check_output(["git", "show", f"{ref}:{path}"], text=True)
    except subprocess.CalledProcessError:
        return None
    return raw.splitlines()


def hunks_by_file(before: str, after: str) -> dict[str, list[tuple[int, int, int, int]]]:
    """Return {file_path: [(old_start, old_count, new_start, new_count), ...]}."""
    try:
        raw = subprocess.check_output(
            ["git", "diff", "--unified=0", before, after, "--", "config_templates"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return {}

    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    by_file: dict[str, list[tuple[int, int, int, int]]] = {}
    current_file = None
    for line in raw.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :]
            by_file.setdefault(current_file, [])
            continue
        match = hunk_re.match(line)
        if match and current_file:
            old_start, old_count, new_start, new_count = match.groups()
            by_file[current_file].append(
                (int(old_start), int(old_count or "1"), int(new_start), int(new_count or "1"))
            )
    return by_file


def json_blocks(lines: list[str], services: set[str]) -> list[tuple[int, int, str]]:
    """Find each service's `"name": { ... }` block inside a servers/mcpServers
    container. Returns [(start_line, end_line, service), ...], 1-based inclusive."""
    blocks: list[tuple[int, int, str]] = []
    container_depth: int | None = None
    depth = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if container_depth is None:
            if CONTAINER_RE.match(line):
                depth += line.count("{") - line.count("}")
                container_depth = depth
            i += 1
            continue
        if depth == container_depth:
            match = KEY_RE.match(line)
            if match and match.group(1) in services:
                service = match.group(1)
                start = i + 1  # 1-based
                block_depth = depth + (line.count("{") - line.count("}"))
                j = i + 1
                cur = block_depth
                while j < n and cur > container_depth:
                    cur += lines[j].count("{") - lines[j].count("}")
                    j += 1
                blocks.append((start, j, service))
                depth = cur
                i = j
                continue
        depth += line.count("{") - line.count("}")
        i += 1
    return blocks


def markdown_blocks(lines: list[str], services: set[str]) -> list[tuple[int, int, str]]:
    """Split on `#`-headings; each section is attributed to whichever service its
    own `claude mcp add-json lvx-<service>` line registers."""
    heading_starts = [i for i, line in enumerate(lines) if HEADING_RE.match(line)]
    if not heading_starts:
        return []
    bounds = heading_starts + [len(lines)]
    blocks: list[tuple[int, int, str]] = []
    for idx in range(len(heading_starts)):
        start0, end0 = bounds[idx], bounds[idx + 1]
        section_text = "\n".join(lines[start0:end0])
        match = ADD_JSON_RE.search(section_text)
        if match and match.group(1) in services:
            blocks.append((start0 + 1, end0, match.group(1)))
    return blocks


def blocks_for(lines: list[str] | None, services: set[str]) -> list[tuple[int, int, str]]:
    if not lines:
        return []
    if any(CONTAINER_RE.match(line) for line in lines):
        return json_blocks(lines, services)
    if any(HEADING_RE.match(line) for line in lines):
        return markdown_blocks(lines, services)
    return []


def service_at_line(blocks: list[tuple[int, int, str]], line_no: int) -> str | None:
    for start, end, service in blocks:
        if start <= line_no <= end:
            return service
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--services", nargs="+", required=True, help="compose service names, e.g. lvx-jira")
    parser.add_argument("--github-output", required=True)
    args = parser.parse_args()

    files = changed_files(args.before, args.after)
    services = {svc.removeprefix("lvx-") for svc in args.services}
    service_map: dict[str, list[str]] = {}

    if files:
        hunks = hunks_by_file(args.before, args.after)
        for file_path, file_hunks in hunks.items():
            before_lines = file_lines(args.before, file_path)
            after_lines = file_lines(args.after, file_path)
            before_blocks = blocks_for(before_lines, services)
            after_blocks = blocks_for(after_lines, services)

            attributed: set[str] = set()
            for old_start, old_count, new_start, new_count in file_hunks:
                for line_no in range(old_start, old_start + old_count):
                    service = service_at_line(before_blocks, line_no)
                    if service:
                        attributed.add(service)
                for line_no in range(new_start, new_start + new_count):
                    service = service_at_line(after_blocks, line_no)
                    if service:
                        attributed.add(service)

            if not attributed and not before_blocks and not after_blocks:
                # Unrecognized file shape -- fall back to whole-file substring match
                # so the file isn't silently dropped from every notification.
                haystack = "\n".join((after_lines or before_lines or []))
                attributed = {svc for svc in services if f"lvx-{svc}" in haystack}

            for service in attributed:
                service_map.setdefault(service, []).append(file_path)

    with open(args.github_output, "a", encoding="utf-8") as gh_out:
        gh_out.write(f"changed_count={len(files)}\n")
        gh_out.write(f"service_map={json.dumps(service_map)}\n")

    if service_map:
        print("Config template changes detected for: " + ", ".join(sorted(service_map)))
    else:
        print("No config template changes detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

