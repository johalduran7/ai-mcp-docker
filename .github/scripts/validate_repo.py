#!/usr/bin/env python3
"""validate_repo.py — mandatory PR-check validator for the MCP marketplace repo.

Fails (non-zero exit) if a commit would:
  * break YAML/JSON/Python syntax anywhere in the repo,
  * add a server folder that's missing its README,
  * leave docker-compose.yml / Dockerfile / README.md / servers/README.md /
    any config_templates/* file out of sync with the actual servers/ folders.

Server implementation is language-agnostic: nothing here requires Python,
a specific dependency manifest, or a specific entrypoint filename. Every
server still needs a matching runtime-<name>/versioned-<name> Docker stage
pair and an lvx-<name> Compose service — CI proves the container actually
speaks MCP with a stdio handshake (validate_mcp_protocol.py), not by
inspecting source files.

This is intentionally dependency-light (stdlib + PyYAML only) so it runs
fast as a required PR check. Every check appends to `FAILURES`; the script
prints a full report (with a per-check PASS/FAIL line) and exits 1 if
anything failed, so a single run tells the author everything that's wrong
instead of failing on the first issue.
"""
from __future__ import annotations

import json
import py_compile
import re
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVERS_DIR = REPO_ROOT / "servers"
REQUIRED_SERVER_FILES = ("README.md",)
# Exact set of per-OS template files every host directory must contain (see dev-manual.md Step 4).
CONFIG_TEMPLATE_FILES = {
    "claude-desktop": (
        "claude_desktop_config.macos.example.json",
        "claude_desktop_config.linux.example.json",
        "claude_desktop_config.windows.example.json",
    ),
    "vscode": (
        "mcp.json.macos.example",
        "mcp.json.linux.example",
        "mcp.json.windows.example",
    ),
    "claude-cli": (
        "claude_cli_mcp_add.macos.example.md",
        "claude_cli_mcp_add.linux.example.md",
        "claude_cli_mcp_add.windows.example.md",
    ),
}

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def discover_servers() -> list[str]:
    if not SERVERS_DIR.is_dir():
        fail("servers/ directory does not exist")
        return []
    names = sorted(
        p.name for p in SERVERS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if not names:
        fail("no server folders discovered under servers/")
    return names


# ── 1. Syntax checks (YAML / JSON / Python) ─────────────────────────────────

def check_yaml_syntax() -> None:
    for path in REPO_ROOT.rglob("*.yml"):
        _check_one_yaml(path)
    for path in REPO_ROOT.rglob("*.yaml"):
        _check_one_yaml(path)


def _check_one_yaml(path: Path) -> None:
    if ".git" in path.parts:
        return
    try:
        yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        fail(f"invalid YAML: {path.relative_to(REPO_ROOT)}: {exc}")


def check_json_syntax() -> None:
    for path in REPO_ROOT.rglob("*.json"):
        if ".git" in path.parts:
            continue
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            fail(f"invalid JSON: {path.relative_to(REPO_ROOT)}: {exc}")


def check_python_syntax() -> None:
    for path in REPO_ROOT.rglob("*.py"):
        if ".git" in path.parts or ".venv" in path.parts:
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                py_compile.compile(str(path), cfile=str(Path(tmp) / "out.pyc"), doraise=True)
        except py_compile.PyCompileError as exc:
            fail(f"invalid Python syntax: {path.relative_to(REPO_ROOT)}: {exc}")


# ── 2. Per-server folder structure ──────────────────────────────────────────

def check_server_structure(servers: list[str]) -> None:
    """Check the language-agnostic filesystem contract for server folders.

    Uniqueness of the lvx-<name> identity is enforced by folder names being
    unique (the filesystem) plus check_docker_compose matching each folder
    to its own Compose service — no source-file convention is required.
    """
    for name in servers:
        server_dir = SERVERS_DIR / name
        for required in REQUIRED_SERVER_FILES:
            if not (server_dir / required).exists():
                fail(f"servers/{name}/{required} is missing (required for every MCP server)")


# ── 3. docker-compose.yml ────────────────────────────────────────────────────

def check_docker_compose(servers: list[str]) -> None:
    compose_path = REPO_ROOT / "docker-compose.yml"
    if not compose_path.is_file():
        fail("docker-compose.yml not found at repo root")
        return
    try:
        compose = yaml.safe_load(compose_path.read_text()) or {}
    except yaml.YAMLError as exc:
        fail(f"docker-compose.yml is not valid YAML: {exc}")
        return

    services = compose.get("services") or {}
    expected = {f"lvx-{name}" for name in servers}
    actual = set(services.keys())

    for missing in sorted(expected - actual):
        fail(f"docker-compose.yml is missing a service for '{missing}' (servers/{missing.removeprefix('lvx-')}/ exists but no compose service)")
    for stale in sorted(actual - expected):
        fail(f"docker-compose.yml has service '{stale}' but there is no matching servers/{stale.removeprefix('lvx-')}/ folder (stale entry?)")

    image_re = re.compile(r"^ghcr\.io/\$\{GHCR_OWNER:-[^}]+\}/\$\{GHCR_REPO:-[^}]+\}/(lvx-[a-z0-9_-]+):\$\{GHCR_TAG:-[^}]+\}$")
    for svc_name, svc in services.items():
        svc = svc or {}
        server_name = svc_name.removeprefix("lvx-")
        image = (svc or {}).get("image", "")
        m = image_re.match(image or "")
        if not m:
            fail(
                f"docker-compose.yml service '{svc_name}' has image '{image}', expected the pattern "
                "ghcr.io/${GHCR_OWNER:-<owner>}/${GHCR_REPO:-<repo>}/lvx-<name>:${GHCR_TAG:-<tag>} "
                "(repo segment namespaces the package so it can't collide with a same-named package "
                "from another repo in the org)"
            )
        elif m.group(1) != svc_name:
            fail(f"docker-compose.yml service '{svc_name}' publishes image for '{m.group(1)}' — name mismatch")

        target = (svc.get("build") or {}).get("target", "")
        expected_target = f"versioned-{server_name}"
        if target != expected_target:
            fail(
                f"docker-compose.yml service '{svc_name}' builds target '{target}', "
                f"expected '{expected_target}'"
            )



# ── 4. Dockerfile stages ────────────────────────────────────────────────────

def check_dockerfile(servers: list[str]) -> None:
    dockerfile_path = REPO_ROOT / "Dockerfile"
    if not dockerfile_path.is_file():
        fail("Dockerfile not found at repo root")
        return
    text = dockerfile_path.read_text()

    # Without this LABEL, images pushed to GHCR sit unlinked from the repo
    # (no README/PR/commit trail on the package page). Must be present in
    # the base stage so every runtime-*/versioned-* stage inherits it.
    if not re.search(
        r'LABEL\s+org\.opencontainers\.image\.source\s*=\s*"https://github\.com/johalduran7/ai-mcp-docker"',
        text,
    ):
        fail(
            'Dockerfile is missing LABEL org.opencontainers.image.source='
            '"https://github.com/johalduran7/ai-mcp-docker" in the base stage '
            "(required so GHCR packages stay linked to this repo)"
        )

    for name in servers:
        if not re.search(rf"\bAS\s+runtime-{re.escape(name)}\b", text):
            fail(f"Dockerfile is missing a 'runtime-{name}' stage for servers/{name}/")
        if not re.search(rf"\bAS\s+versioned-{re.escape(name)}\b", text):
            fail(f"Dockerfile is missing a 'versioned-{name}' stage for servers/{name}/")


# ── 5. READMEs reference every server ───────────────────────────────────────

def check_readmes(servers: list[str]) -> None:
    for readme_rel in ("README.md", "servers/README.md"):
        path = REPO_ROOT / readme_rel
        if not path.is_file():
            fail(f"{readme_rel} not found")
            continue
        text = path.read_text()
        for name in servers:
            if f"lvx-{name}" not in text:
                fail(f"{readme_rel} does not mention 'lvx-{name}' — add it to the server table")


# ── 6. config_templates mention every server ────────────────────────────────

def check_config_templates(servers: list[str]) -> None:
    templates_dir = REPO_ROOT / "config_templates"
    if not templates_dir.is_dir():
        fail("config_templates/ not found")
        return
    for tool_dir_name, filenames in CONFIG_TEMPLATE_FILES.items():
        tool_dir = templates_dir / tool_dir_name
        if not tool_dir.is_dir():
            fail(f"config_templates/{tool_dir_name}/ not found")
            continue
        for filename in filenames:
            file_path = tool_dir / filename
            if not file_path.is_file():
                fail(
                    f"config_templates/{tool_dir_name}/{filename} not found "
                    "(macOS, Linux, and Windows variants are all required)"
                )
                continue
            text = file_path.read_text()
            for name in servers:
                if f"lvx-{name}" not in text:
                    fail(
                        f"config_templates/{tool_dir_name}/{filename} does not mention 'lvx-{name}' — "
                        "every server must appear in every OS variant of every host's config templates"
                    )


def main() -> int:
    servers = discover_servers()
    print(f"Discovered {len(servers)} server(s): {', '.join(servers)}")

    checks: tuple[tuple[str, object], ...] = (
        ("YAML syntax", check_yaml_syntax),
        ("JSON syntax", check_json_syntax),
        ("Python syntax", check_python_syntax),
        ("Server folders", lambda: check_server_structure(servers)),
        ("docker-compose.yml naming", lambda: check_docker_compose(servers)),
        ("Dockerfile stages", lambda: check_dockerfile(servers)),
        ("README coverage", lambda: check_readmes(servers)),
        ("config_templates coverage", lambda: check_config_templates(servers)),
    )
    for label, check in checks:
        before = len(FAILURES)
        check()
        status = "PASS" if len(FAILURES) == before else "FAIL"
        print(f"[{status}] {label}")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):\n")
        for f in FAILURES:
            print(f"  - {f}")
        return 1

    print("All structure checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
