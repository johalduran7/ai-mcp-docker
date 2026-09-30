#!/usr/bin/env python3
"""Verify that Compose services accept an MCP initialize request.

By default checks every service in docker-compose.yml. Pass service names
as CLI args (e.g. from detect_touched_services.py's output) to scope the
check to just those services instead."""
from __future__ import annotations

import json
import selectors
import subprocess
import sys
import time

REQUEST = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ci-validator", "version": "1.0"},
        },
    }
) + "\n"
TIMEOUT_SECONDS = 30


def services(explicit: list[str] | None = None) -> list[str]:
    if explicit:
        return sorted(explicit)
    result = subprocess.run(
        ["docker", "compose", "config", "--services"],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(line for line in result.stdout.splitlines() if line)


def check_service(service: str) -> str | None:
    process = subprocess.Popen(
        ["docker", "compose", "run", "--rm", "-T", "--no-deps", service],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin and process.stdout and process.stderr
    process.stdin.write(REQUEST)
    process.stdin.flush()

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    response: dict | None = None
    try:
        while time.monotonic() < deadline:
            events = selector.select(deadline - time.monotonic())
            if not events:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 1 and message.get("jsonrpc") == "2.0":
                response = message
                break
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    if response and isinstance(response.get("result"), dict):
        return None
    stderr = process.stderr.read().strip().replace("\n", " ")
    return stderr[:500] or "did not return a valid MCP initialize response"


def main() -> int:
    failures = []
    for service in services(sys.argv[1:] or None):
        error = check_service(service)
        if error:
            failures.append(f"{service}: {error}")
        else:
            print(f"[PASS] MCP stdio initialize: {service}")
    if failures:
        print("\nMCP protocol failures:", file=sys.stderr)
        print(*failures, sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
