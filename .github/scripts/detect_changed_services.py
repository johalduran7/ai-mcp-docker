#!/usr/bin/env python3
"""detect_changed_services.py — source-hash diff used by the publish pipeline.

Hashes the SOURCE FILES that go into each service's image, not the built
image itself — same source → same hash → skip the build/push entirely. This
is Docker-independent, fast (no builds needed for the check), and skips
unchanged services completely instead of rebuilding them just to discover
they're unchanged.

For each compose service, hashes (in this order):
  * every file under servers/<name>/**            (sorted, relative paths;
                                                    includes that service's
                                                    own requirements.txt —
                                                    each runtime-<name>
                                                    Dockerfile stage installs
                                                    its deps independently)
  * resources/version_notifier.py                 (shared by every image)
  * Dockerfile / .dockerignore
  * that service's own resolved docker-compose.yml block (env, volumes,
    command, ports, etc.) — via `docker compose config --format json`, so
    a compose-only edit to one service (or a brand new service added) is
    caught even when no file under servers/<name>/** changed.

...and compares the result against the "sha" field already recorded for that
service in versions/mcp-version.json. Missing or mismatched => changed.

Usage:
    python3 detect_changed_services.py --versions-file versions/mcp-version.json \
        --services mcp-jira mcp-confluence ... \
        --github-output "$GITHUB_OUTPUT"

Prints a JSON report to stdout and (optionally) appends `changed` (a JSON
array of service names) and `changed_count` to the file named by
--github-output, in the format GitHub Actions expects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# HOME differs across every environment (dev laptop, CI runner) and is the
# root of every "${HOST_X_DIR:-${HOME}/...}"-style volume default in
# docker-compose.yml -- including ones added after this script was last
# touched. Pinning it here (instead of enumerating each HOST_*_DIR variable
# by name) makes any such default resolve to the same plain path everywhere,
# with no changes needed here when a new volume mount is added. DOCKER_CONFIG
# is pinned to the *real* ~/.docker so Docker CLI can still find the
# `compose` plugin under cli-plugins/ -- overriding HOME alone breaks that
# lookup entirely.
_real_home = os.environ.get("HOME", "")
_PINNED_ENV = {
    "HOME": "/pinned-home",
    "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG") or f"{_real_home}/.docker",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_files(paths: list[Path]) -> str:
    """Hash the concatenation of each file's own sha256, in the given order."""
    h = hashlib.sha256()
    for p in paths:
        h.update(_sha256_file(p).encode("utf-8"))
    return h.hexdigest()


def _compose_service_config(repo_root: Path, service: str) -> dict:
    """That service's resolved docker-compose.yml block (env/volumes/
    command/ports/etc.). Uses `--format json` instead of the default YAML
    so this script needs no extra dependency (PyYAML isn't installed in
    publish.yml's job)."""
    return _resolved_compose_config(repo_root).get(service) or {}


@lru_cache(maxsize=None)
def _resolved_compose_config(repo_root: Path) -> dict:
    """All services' resolved docker-compose.yml blocks in one call,
    cached since main() calls source_sha_for() (and thus this) once per
    service with the same repo_root. Uses full interpolation with
    _PINNED_ENV overriding the host-dependent variables, so every volume
    resolves to a concrete, deterministic path — the hash must depend only
    on committed file content, not runtime/ambient env vars, but still
    needs a value Compose can actually parse as a volume spec. Raises
    RuntimeError on failure instead of returning {} silently, since a
    silent empty result would let a real compose-driven change go
    undetected."""
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=repo_root, capture_output=True, text=True,
        env={**os.environ, **_PINNED_ENV},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`docker compose config` failed: stderr={result.stderr.strip()!r} "
            f"stdout={(result.stdout or '').strip()!r}"
        )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON from `docker compose config`: {exc}: {(result.stdout or '')[:200]!r}"
        ) from exc
    return data.get("services") or {}


def source_sha_for(repo_root: Path, service: str) -> str:
    suffix = service.removeprefix("lvx-")
    server_dir = repo_root / "servers" / suffix
    if not server_dir.is_dir():
        raise FileNotFoundError(f"servers/{suffix} does not exist for service '{service}'")

    server_files = sorted(p for p in server_dir.rglob("*") if p.is_file())
    shared = [repo_root / "resources" / "version_notifier.py"]
    dockerfile = [repo_root / "Dockerfile", repo_root / ".dockerignore"]

    all_files = server_files + shared + dockerfile
    combined = _sha256_files(all_files)

    compose_repr = json.dumps(_compose_service_config(repo_root, service), sort_keys=True)
    h = hashlib.sha256(combined.encode("utf-8"))
    h.update(compose_repr.encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--versions-file", required=True, help="path to versions/mcp-version.json")
    parser.add_argument("--services", nargs="+", required=True, help="compose service names, e.g. mcp-jira mcp-aws")
    parser.add_argument("--repo-root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--github-output", default="", help="path to $GITHUB_OUTPUT to append to")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    versions_path = Path(args.versions_file)
    remote = {}
    if versions_path.exists():
        remote = json.loads(versions_path.read_text() or "{}")

    changed: list[str] = []
    local_sha: dict[str, str] = {}
    report: dict[str, dict] = {}

    for svc in sorted(args.services):
        sha = source_sha_for(repo_root, svc)
        local_sha[svc] = sha
        remote_sha = (remote.get(svc) or {}).get("sha", "")
        is_changed = remote_sha != sha
        if is_changed:
            changed.append(svc)
        report[svc] = {"local_sha": sha, "remote_sha": remote_sha or None, "changed": is_changed}

    print(json.dumps({"changed": changed, "services": report}, indent=2))

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"changed={json.dumps(changed)}\n")
            fh.write(f"changed_count={len(changed)}\n")
            fh.write(f"local_sha={json.dumps(local_sha)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
