#!/usr/bin/env python3
"""detect_touched_services.py — git-diff-based "what changed" check for the
validate (PR) pipeline.

Deliberately independent of publish.yml: no dependency on the `versions`
branch or any previously-published SHA registry. Answers only "did this PR
touch this service's own files, or something shared by all of them?",
based on a plain git diff between the PR's base branch and HEAD.

Trigger paths kept in sync with detect_changed_services.py's
source_sha_for() (the publish pipeline's hash inputs) so "what counts as
this service's inputs" means the same thing in both pipelines:
  * servers/<name>/**              -> only that service (includes that
                                     service's own requirements.txt — each
                                     runtime-<name> Dockerfile stage installs
                                     its deps independently, so it's not a
                                     shared input)
  * resources/version_notifier.py  -> every service (shared import)
  * Dockerfile / .dockerignore     -> every service (shared base stage +
                                     build context)

Also treats the validate pipeline's own files as shared triggers
(validate.yml, validate_mcp_protocol.py, and this script) — otherwise a
change to the scoping logic itself could report changed_count=0 and skip
exercising the very code path it just modified.

docker-compose.yml is NOT a shared trigger (a new server touches it but
shouldn't force a rebuild of the other 7). Instead its *resolved* per-service
config is diffed directly against the base ref via `docker compose config`,
so only the service(s) whose actual env/volumes/command/etc. changed get
flagged — catching compose-only edits to an existing service that don't
touch that service's own servers/<name>/** folder.

Usage:
    python3 detect_touched_services.py --base-ref origin/main \
        --services lvx-jira lvx-confluence ... \
        --github-output "$GITHUB_OUTPUT"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

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


def changed_files(base_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _merge_base(base_ref: str) -> str:
    """The commit where HEAD actually diverged from base_ref — matches the
    `base_ref...HEAD` (triple-dot) semantics changed_files() already uses,
    so unrelated commits landed on base_ref after the branch forked don't
    count as part of this PR's diff."""
    result = subprocess.run(
        ["git", "merge-base", base_ref, "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _resolved_compose_services(compose_file: str | None) -> dict:
    """Run `docker compose config` against a given compose file path (None
    = the default docker-compose.yml in the cwd) and return its `services`
    mapping. Uses full interpolation with _PINNED_ENV overriding the
    host-dependent variables, so every volume resolves to a concrete,
    deterministic path — the diff must depend only on committed file
    content, not runtime env, but still needs a value Compose can actually
    parse as a volume spec. Raises RuntimeError on failure instead of
    returning {} silently, since that would let a real compose-driven
    change (or a PR that broke docker-compose.yml outright) go undetected."""
    cmd = ["docker", "compose"]
    if compose_file:
        cmd += ["-f", compose_file]
    cmd += ["config"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, env={**os.environ, **_PINNED_ENV}
    )
    if result.returncode != 0:
        raise RuntimeError(
            "`docker compose config` failed: "
            f"stderr={result.stderr.strip()!r}; stdout={result.stdout.strip()!r}"
        )
    try:
        return (yaml.safe_load(result.stdout) or {}).get("services") or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid YAML from `docker compose config`: {exc}") from exc


def services_with_compose_diff(base_ref: str) -> set[str]:
    """Service names whose *resolved* docker-compose.yml block differs
    between the merge-base of base_ref/HEAD and HEAD (new services
    included). Diffs against the merge-base, not base_ref's current tip,
    so it stays aligned with changed_files()'s diff semantics."""
    head_services = _resolved_compose_services(None)

    merge_base_sha = _merge_base(base_ref)
    show = subprocess.run(
        ["git", "show", f"{merge_base_sha}:docker-compose.yml"],
        capture_output=True, text=True,
    )
    if show.returncode != 0:
        # No base version to compare against -> every current service counts.
        return set(head_services)

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, dir=".") as tmp:
        tmp.write(show.stdout)
        tmp_path = tmp.name
    try:
        base_services = _resolved_compose_services(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {
        name for name, cfg in head_services.items()
        if cfg != base_services.get(name)
    }


def is_shared_trigger(path: str) -> bool:
    # Includes this script itself — a bug in the scoping logic must not be
    # able to hide its own blast radius by reporting changed_count=0.
    return path in (
        "Dockerfile",
        ".dockerignore",
        "resources/version_notifier.py",
        ".github/workflows/validate.yml",
        ".github/scripts/validate_mcp_protocol.py",
        ".github/scripts/detect_touched_services.py",
    )


def service_for_path(path: str) -> str | None:
    # >= 3 segments: servers/<name>/<something>. A bare servers/<name> or a
    # top-level file directly under servers/ (e.g. servers/README.md) isn't
    # any single service's input.
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "servers":
        return f"lvx-{parts[1]}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", required=True, help="e.g. origin/main")
    parser.add_argument("--services", nargs="+", required=True, help="compose service names")
    parser.add_argument("--github-output", default="", help="path to $GITHUB_OUTPUT to append to")
    args = parser.parse_args()

    files = changed_files(args.base_ref)
    shared_touch = any(is_shared_trigger(f) for f in files)

    if shared_touch:
        changed = sorted(args.services)
    else:
        touched = {service_for_path(f) for f in files}
        touched.discard(None)
        if "docker-compose.yml" in files:
            touched |= services_with_compose_diff(args.base_ref)
        changed = sorted(s for s in args.services if s in touched)

    print(json.dumps(
        {"changed_files": files, "shared_touch": shared_touch, "changed": changed},
        indent=2,
    ))

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"changed={json.dumps(changed)}\n")
            fh.write(f"changed_count={len(changed)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
