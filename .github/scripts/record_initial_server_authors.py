#!/usr/bin/env python3
"""Record the opening PR author for newly created MCP server folders."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def new_servers(base_ref: str) -> set[str]:
    base_servers = set(git_output("ls-tree", "-d", "--name-only", f"{base_ref}:servers").splitlines())
    current_servers = {
        path.name
        for path in Path("servers").iterdir()
        if path.is_dir() and (path / "server.py").is_file()
    }
    return current_servers - base_servers


def record_author(readme: Path, server: str, author: str, author_name: str) -> bool:
    text = readme.read_text()
    pattern = re.compile(
        rf"^(\| \[{re.escape(server)}\]\(servers/{re.escape(server)}/README\.md\) \| [^|]+)(?: \| ([^|]*))? \|$",
        re.MULTILINE,
    )

    # `server` is only reachable here via new_servers(), i.e. this is the
    # server's first commit — always force the real PR author, overwriting
    # any placeholder or pre-filled (possibly wrong) name someone else typed.
    def replace(match: re.Match[str]) -> str:
        return f"{match.group(1)} | [{author_name}](https://github.com/{author}) |"

    updated = pattern.sub(replace, text)
    if updated == text:
        return False
    readme.write_text(updated)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--author-name", required=True)
    args = parser.parse_args()

    readme = Path("README.md")
    changed = [
        server
        for server in sorted(new_servers(args.base_ref))
        if record_author(readme, server, args.author, args.author_name)
    ]
    print("Recorded author for: " + ", ".join(changed) if changed else "No new MCP authors to record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())