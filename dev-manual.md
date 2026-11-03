# MCP Docker Registry: Contributor Manual

This project packages MCP servers as Docker images for the MCP Docker Registry.
The server may be implemented in Python, Node.js, Django, Go, or another
runtime. The integration standard is the container contract: the image must run
the MCP server over standard input/output and must not write protocol logs to
standard output.

## Choose Your Path

| I want to... | Path | Result |
|---|---|---|
| Add a brand-new MCP, change shared project files, or ship a change straight to everyone | [Path A: Contributor Workflow](#path-a-contributor-workflow) | Merged PR publishes `:latest` |
| Develop a new version of an **existing** MCP and let the team test it before it reaches `latest` | [Path B: Versioned Dev Workflow](#path-b-versioned-dev-workflow-new-version-of-an-existing-mcp) | Branch push publishes `:v<N>`; `:latest` is untouched until the PR merges |

## Path A: Contributor Workflow

1. Clone and branch.
   ```bash
   git clone https://github.com/johalduran7/ai-mcp-docker.git
   cd ai-mcp-docker
   git checkout -b your-new-branch   # name doesn't matter
   ```
2. Fill in the header variables of the [Copilot prompt](#prompt-for-copilot) below.
3. Paste the prompt into Copilot. It wires and integrates the MCP into the framework.
4. Build the image.
   ```bash
   docker compose build lvx-<name>
   ```
5. Add the new server to your AI host's config for your OS:
   - **Claude Desktop** — [macOS](config_templates/claude-desktop/claude_desktop_config.macos.example.json) · [Linux](config_templates/claude-desktop/claude_desktop_config.linux.example.json) · [Windows (WSL2)](config_templates/claude-desktop/claude_desktop_config.windows.example.json)
   - **VS Code Copilot** — [macOS](config_templates/vscode/mcp.json.macos.example) · [Linux](config_templates/vscode/mcp.json.linux.example) · [Windows (WSL2)](config_templates/vscode/mcp.json.windows.example)
   - **Claude CLI** — [macOS](config_templates/claude-cli/claude_cli_mcp_add.macos.example.md) · [Linux](config_templates/claude-cli/claude_cli_mcp_add.linux.example.md) · [Windows (WSL2)](config_templates/claude-cli/claude_cli_mcp_add.windows.example.md)
6. Restart the AI host, confirm the `lvx-<name>` container starts, and call one tool.
7. If it works, push it to GitHub.
   ```bash
   git status
   git add .
   git commit -m "added <name> server"
   git push --set-upstream origin your-new-branch
   ```
8. Create a PR into `main`.
9. Verify the `validate` pipeline passes and request a peer reviewer.
10. Merge.

## Prompt for Copilot

Fill in **only the header variables** at the top, then paste the complete prompt
into Copilot. Do not edit the instruction body — it references the header values
automatically.

```text
# ── USER FILLS THESE VARIABLES ───────────────────────────────────────────────
# MCP_NAME: <MCP_NAME>
# SOURCE_PATH: <SOURCE_PATH>  (absolute path to the existing MCP source folder)
# OperatingSystem: <linux|Windows|Macos>  (the OS you'll build/test this on)
# Required token/secret environment variables: <TOKEN_NAME>, <ANOTHER_SECRET>
#   (write "none" if no secrets are needed)
# Required non-secret environment variables: <VAR_NAME=description>
#   (write "none" if no extra env vars are needed)
# Network/access prerequisite: <VPN, firewall rule, IAM role, etc. or "none">
# ── DO NOT EDIT BELOW THIS LINE ───────────────────────────────────────────────

Integrate the source MCP at SOURCE_PATH above into the ai-mcp-docker registry at
ai-mcp-docker/servers/<MCP_NAME>/. The Docker Compose service and image name are
derived as `lvx-<MCP_NAME>`; do not request a separate Compose service name.
Before editing, verify <MCP_NAME> is not already present in
`ai-mcp-docker/servers/`, `docker-compose.yml`, or the host configuration
templates. Inspect the existing servers, Dockerfile, docker-compose.yml, and
all AI-host config templates. Wire it into the project just like the other
MCPs already in this registry.

Preserve the server's implementation language and protocol internally, but
package it as a linux/amd64 Docker image that exposes MCP over stdio. Add the
`runtime-<MCP_NAME>` and `versioned-<MCP_NAME>` Docker stages,
`lvx-<MCP_NAME>` Compose service, documentation, and all six host config
templates.

Critical handshake requirement: the container's entrypoint MUST actually start
the stdio transport loop, not just construct the server object and register
tools. For a Python/FastMCP server this means ending `server.py` with
`if __name__ == "__main__": mcp.run(transport="stdio")` (verify every existing
`servers/*/server.py` does this and match it). For any other language, call
that SDK's equivalent stdio-connect entrypoint (e.g. the Node MCP SDK's
`server.connect(new StdioServerTransport())`). Without this call the process
registers tools and exits/idles without ever reading stdin, and
`validate_mcp_protocol.py` will fail with a timeout that gives no obvious clue
as to the missing line.

Upgrade-checking rule: register `check_for_updates` in every server with
`_install_version_notifier(mcp, "lvx-<MCP_NAME>")`. For upgrade requests, the
AI host calls GitHub's `check_for_updates` first; GitHub fetches the fresh
private registry, then the host passes its `versions` result to each MCP's
notifier tool for a local `BUILD_VERSION` comparison.

Credentials rule: if "Required token/secret environment variables" is "none",
do NOT add any `-e TOKEN`, `env:`, or prompt-driven input blocks to any config
template or Compose service. If tokens are declared, define each as a container
environment variable and expose it as a secure, prompt-driven input in every
VS Code, Claude Desktop, and Claude CLI configuration file. Never hardcode
secrets.

Expose every non-secret environment variable declared in "Required non-secret
environment variables" to the container via the Compose service `environment`
block and each config template `env` block. If "none", omit those blocks.

Document the network/access prerequisite declared above in the server README
and in every config template comment where relevant.

Regardless of the declared OperatingSystem, update all nine per-OS host
configuration templates — macOS, Linux, and Windows variants for VS Code,
Claude Desktop, and Claude CLI — so every platform can consume the new
server. OperatingSystem only tells you which one the contributor will
build/test locally; it does not limit which templates get the new entry.
Add `--network host` only to the Linux-variant templates, and only when the
declared network/access prerequisite requires it (VPN-only internal hosts);
leave the macOS and Windows variants without it.

Do not change unrelated servers. Do not run Docker builds or smoke tests; the
contributor runs the manual validation steps after reviewing the changes. Show
the exact files changed and any required permissions or external prerequisites.

If the source MCP is an HTTP, SSE, Streamable HTTP, Django, or other network
service, add a stdio MCP adapter as the container entrypoint without changing
the existing service API.
```

The rest of this manual explains the standard and the changes that prompt asks
Copilot to make.

## Build and Test After Copilot Integrates the Server

Build the new MCP locally while disconnected from the corporate VPN. The VPN
can block Docker Hub access required for image pulls and builds:

```bash
docker compose build lvx-<name>
```

Docker Desktop and Colima both require the Docker Buildx plugin for Compose
builds. Verify it first with `docker buildx version`; install `docker-buildx`
with Homebrew if needed. When using Colima, enable amd64 emulation because
these images target `linux/amd64` (for example, start Colima with Rosetta
enabled).

Then complete this local verification flow:

1. Add the generated server configuration to the file for your AI host:
	`.vscode/mcp.json` for VS Code Copilot,
	`claude_desktop_config.json` for Claude Desktop, or the generated
	`claude mcp add-json` command for Claude CLI.
2. Supply the required token values through that host's secure input or
	environment-variable configuration.
3. Restart the AI host so it starts the new MCP container.
4. Run `docker ps` and confirm an `lvx-<name>` container is running.
5. Confirm the server appears in the AI host's MCP list and call one tool.

## Upgrade-checking Architecture

Every MCP registers `check_for_updates` and owns its own local `BUILD_VERSION`.
For an upgrade request, the AI host first calls the GitHub MCP's
`check_for_updates`. GitHub is the sole authenticated, no-cache reader of the
private version registry and returns the fresh `versions` manifest. The host
passes that manifest to each selected MCP's `check_for_updates` tool, which
compares only its own local version. This keeps the GitHub token in one MCP,
avoids stale raw-content caching, and does not require a hardcoded MCP roster.

## What Copilot Wires for You

You do not need to assemble this wiring by hand. The Copilot prompt creates it
for the new server named `<name>`. Use this section to understand what is being
added and to review the resulting pull request.

The server source lives here:

```text
servers/<name>/
	server.py or equivalent entrypoint
	requirements.txt / package.json / runtime dependency manifest
	README.md
	run_server.sh                 # only when the runtime needs a wrapper
```

Copilot connects that source to the registry in four places:

1. Add its runtime and versioned build stages to `Dockerfile`.
2. Add an `lvx-<name>` service to `docker-compose.yml`.
3. Add it to the VS Code, Claude Desktop, and Claude CLI example configs.
4. Add it to the server tables in `README.md` and `servers/README.md`.

Existing servers under `servers/` are the reference pattern Copilot should
follow. Review the generated changes rather than recreating this structure
manually.

## Step 1: Prepare Your Server

Your container command must keep an MCP server running over stdio. It must:

- Read MCP requests from stdin and write protocol responses to stdout.
- Send application logs only to stderr.
- Read credentials and endpoints from environment variables; never hardcode
	secrets in source, Compose, examples, or image layers.
- Exit nonzero when startup configuration is invalid.

If your MCP is HTTP, SSE, Streamable HTTP, Django, or another network service,
add a small stdio MCP adapter as the image entrypoint. The adapter can call the
existing service internally. Consumers of this registry use the stdio Docker
contract, not a host port or a specific implementation language.

## Step 2: Add the Docker Build Stages

This repository has two stages per server:

- `runtime-<name>`: copies runtime code without a version stamp. The release
	helper uses its source hash to detect real changes.
- `versioned-<name>`: extends the runtime stage and adds `BUILD_VERSION` and
	`POLL_INTERVAL_MINUTES`. This is the image pushed to GHCR.

Mirror an existing server in `Dockerfile`:

```dockerfile
FROM base AS runtime-<name>
COPY servers/<name>/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/<name> /app/servers/<name>
CMD ["python", "/app/servers/<name>/server.py"]

FROM runtime-<name> AS versioned-<name>
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
		POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}
```

For a non-Python implementation, choose the correct base/runtime stage and
replace the command. Each server installs its own dependency manifest
independently in its own `runtime-<name>` stage — not merged into `base` —
so one server's dependency change can't invalidate another server's build
cache or force an unrelated rebuild.

## Step 3: Add Compose Service

Add `lvx-<name>` to `docker-compose.yml`. Use the matching
`versioned-<name>` target and a GHCR image name:

```yaml
lvx-<name>:
	platform: linux/amd64
	build:
		context: .
		dockerfile: Dockerfile
		target: versioned-<name>
		args:
			BUILD_VERSION: ${BUILD_VERSION:-v20200101000000}
	image: ghcr.io/${GHCR_OWNER:-johalduran7}/${GHCR_REPO:-ai-mcp-docker}/lvx-<name>:${GHCR_TAG:-latest}
	environment:
		SERVICE_TOKEN: ${SERVICE_TOKEN:-}
	command: ["python", "/app/servers/<name>/server.py"]
	stdin_open: true
	tty: false
```

Use read-only mounts by default. Any writable host mount, especially AWS or
tool configuration, requires a documented reason and explicit review.

## Step 4: Register the AI Host Configurations

Add the new Docker command and required secure inputs to all nine templates
— macOS, Linux, and Windows variants for each of the three clients. Only the
Linux variant ever gets `--network host` (see the credentials/network rules
above):

- `claude-desktop/claude_desktop_config.macos.example.json`
- `claude-desktop/claude_desktop_config.linux.example.json`
- `claude-desktop/claude_desktop_config.windows.example.json`
- `vscode/mcp.json.macos.example`
- `vscode/mcp.json.linux.example`
- `vscode/mcp.json.windows.example`
- `claude-cli/claude_cli_mcp_add.macos.example.md`
- `claude-cli/claude_cli_mcp_add.linux.example.md`
- `claude-cli/claude_cli_mcp_add.windows.example.md`

For VS Code, the standard registration uses Docker with stdio:

```json
"<name>": {
	"type": "stdio",
	"command": "docker",
	"args": [
		"run", "--rm", "-i", "--platform", "linux/amd64",
		"-e", "SERVICE_TOKEN",
		"ghcr.io/johalduran7/ai-mcp-docker/lvx-<name>:latest"
	],
	"env": {
		"SERVICE_TOKEN": "${input:<name>_token}"
	}
}
```

Add a matching password input. Credentials must remain prompt-driven, never
committed to a config file.

## Step 6: Document the Server

Create `servers/<name>/README.md` with:

- What the MCP does and its intended users.
- Environment variables, required permissions, and network dependencies.
- A table of tools, including which actions mutate remote systems.
- Example prompts for normal and failure cases.
- Local build and test command.

Update the root server table and `servers/README.md` in the same change.

## Step 7: Release

Publishing is CI-only, no manual pushes. There are two release channels:

| Channel | Trigger | Published tag | Pipeline |
|---|---|---|---|
| Released | Merge to `main` | `:latest` | `publish` |
| Dev version | Push to `dev-lvx-<name>-v<N>` that changes server files, `Dockerfile` or `docker-compose.yml` | `:v<N>` | `publish-dev` (see [Path B](#path-b-versioned-dev-workflow-new-version-of-an-existing-mcp)) |

For the released channel:

1. Open a PR. The `validate` pipeline builds every image (no push) and must
   pass before merge.
2. Merge to `main`. The `publish` pipeline hashes each service's source,
   builds + pushes only what changed to GHCR, and stamps the version.

A PR opened from a `dev-lvx-<name>-v<N>` branch follows this same released
flow. The `:v<N>` tag it was tested under is not removed automatically; delete
it by hand after the merge (see Path B, step 6).

Locally you only need to confirm your image builds:

```bash
docker compose build lvx-<name>
```

## Path B: Versioned Dev Workflow (new version of an existing MCP)

Use this to work on an MCP that is already published (for example `lvx-jira`)
without changing `:latest`. Each dev branch publishes its own tag, so the team can
pull and try your build while it is still under review.

In this section, `<mcp name>` is the server folder under `servers/` and `<N>` is
your version number. Replace both with your own values everywhere.

Rules enforced by the [publish-dev pipeline](.github/workflows/publish-dev.yml):

- Branch name: `dev-lvx-<mcp name>-v<N>`, where `<N>` is any whole number,
  ideally the next one in sequence (`v1`, then `v2`, then `v3`, and so on, with
  no upper limit). It becomes the image tag: the branch
  `dev-lvx-<mcp name>-v<N>` publishes `lvx-<mcp name>:v<N>`.
  Example: the branch `dev-lvx-grafana-v2` publishes `lvx-grafana:v2`.
  Names that don't follow this (for example `v1.3` or no version) fail the
  pipeline.
- Contributor Workflow branches have no naming rule; just avoid the `dev-lvx-`
  prefix, which is reserved for this path.
- Only existing MCPs: `servers/<mcp name>/` must already be on `main` and
  `ghcr.io/johalduran7/ai-mcp-docker/lvx-<mcp name>:latest` must already exist.
  A dev branch can never create a new package; new MCPs use Path A.
- Publishing is triggered by file changes, not by a keyword: a push that
  changes `servers/**`, `Dockerfile` or `docker-compose.yml` (READMEs and other
  `.md` files excluded) starts the pipeline, and a push that only changes other
  files (docs, `config_templates/`, `.github/`, ...) does not.
- Only the server named in the branch is built and published, and only when
  `servers/<mcp name>/` itself has non-doc changes versus `main`. If that folder
  is untouched (for example only `servers/jira/` changed on a
  `dev-lvx-<mcp name>-v<N>` branch for a different server), the run ends with a
  notice and nothing is built or published. Changes to other servers are ignored
  by this pipeline.
- Publishing never touches `:latest`, `versions/mcp-version.json`, or the
  publish notifications.

Steps:

1. Clone the repo and create your branch from `main`, using the naming pattern
   above.
   ```bash
   git clone https://github.com/johalduran7/ai-mcp-docker.git
   cd ai-mcp-docker
   git checkout -b dev-lvx-<mcp name>-v<N> main
   ```
   Example only: for the `grafana` server, version 2, the command is
   `git checkout -b dev-lvx-grafana-v2 main`. Do not copy it unless that is
   really your MCP and version.
2. Paste the [Copilot prompt for Path B](#prompt-for-copilot-path-b) (or edit
   `servers/<mcp name>/` by hand) and build locally.
   ```bash
   docker compose build lvx-<mcp name>
   ```
3. Commit and push. Every push that changes the files above publishes
   `:v<N>` again.
   ```bash
   git add .
   git commit -m "<describe your change>"
   git push --set-upstream origin dev-lvx-<mcp name>-v<N>
   ```
4. The pipeline validates the named server only (repo checks, hadolint, build,
   MCP handshake, unit tests) and then pushes the `:v<N>` tag. Share the pull
   command with your testers:
   ```bash
   docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-<mcp name>:v<N>
   ```
   Pushing again overwrites `:v<N>`. The container reports
   itself as a developer build, so `check_for_updates` does not track it.

   > **Pull the versioned tag, not `latest`.** `docker pull ...:latest` still
   > gives you the released build without your changes. Testers must pull
   > `...lvx-<mcp name>:v<N>` and change the
   > `image` / Docker args in their AI host config from `:latest` to `:v<N>`,
   > then restart the AI host. Switch back to `:latest` when done testing.
   >
   > The AI host starts whatever tag its config names, so pulling `:v<N>` alone
   > changes nothing. Edit the image argument in your own config
   > (`claude_desktop_config.json`, `.vscode/mcp.json`, or the `claude mcp add-json`
   > command):
   >
   > ```diff
   > -  "ghcr.io/johalduran7/ai-mcp-docker/lvx-<mcp name>:latest"
   > +  "ghcr.io/johalduran7/ai-mcp-docker/lvx-<mcp name>:v<N>"
   > ```
5. When the team has tested it, open a PR into `main`. The existing `validate`
   pipeline gates the PR and, after merge, `publish` releases it to `:latest`
   (Contributor Workflow, steps 8–10).
6. **After the merge, delete your `:v<N>` package version.** The merged branch
   is deleted, so `:v<N>` can never be refreshed again and would keep serving
   an outdated build next to the new `:latest`. This cleanup is manual:
   1. Confirm the `publish` run for your merge finished and `:latest` is updated.
   2. In GitHub, open your `johalduran7` profile, **Packages**, then
      `ai-mcp-docker/lvx-<mcp name>`.
   3. Open **Versions**, find the version tagged `v<N>` and use
      **Delete version**. Do not delete the version tagged `latest`.
   4. Tell your testers to switch their AI host config back to `:latest`.
7. A dev branch only changes the existing MCP: do not add or rename servers,
   and keep changes to shared files (`Dockerfile`, `resources/`) minimal
   because they are reviewed in the PR for every MCP.

### Prompt for Copilot (Path B)

Fill in **only the header variables**, then paste the complete prompt into Copilot.

```text
# ── USER FILLS THESE VARIABLES ───────────────────────────────────────────────
# MCP_NAME: <MCP_NAME>  (an existing folder under servers/)
# VERSION: <N>  (whole number starting at 1, next in sequence; the branch must be dev-lvx-<MCP_NAME>-v<VERSION>)
# Change requested: <what the new version should add or fix>
# OperatingSystem: <linux|Windows|Macos>  (the OS you'll build/test this on)
# ── DO NOT EDIT BELOW THIS LINE ───────────────────────────────────────────────

Verify the current git branch is exactly `dev-lvx-<MCP_NAME>-v<VERSION>` and that
`ai-mcp-docker/servers/<MCP_NAME>/` already exists. If either is false,
stop and tell me instead of continuing.

Implement the "Change requested" above inside
`ai-mcp-docker/servers/<MCP_NAME>/` only. Follow the conventions already used by that
server and by `servers/aws/server.py` for auth. Keep the stdio entrypoint,
`_install_version_notifier(mcp, "lvx-<MCP_NAME>")`, and the existing README tool
table accurate: update that server's README for any tool you add, rename, or remove.

Do not add, rename, or delete servers. Do not change other servers. Do not edit
`Dockerfile`, `docker-compose.yml`, `resources/`, `config_templates/`, or
`.github/` unless the requested change cannot work without it; if it can't,
list exactly what you changed and why. Do not run Docker builds or smoke
tests; I run `docker compose build lvx-<MCP_NAME>` myself.

Finish by showing the files changed and these next steps: commit and push the
branch, then share
`docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-<MCP_NAME>:v<VERSION>`.
```

## Pull Request Checklist

- [ ] Server source and dependency manifest are under `servers/<name>/`.
- [ ] Image uses stdio for its MCP interface and logs only to stderr.
- [ ] The entrypoint actually starts the stdio transport loop (e.g.
      `if __name__ == "__main__": mcp.run(transport="stdio")` for Python, or
      the equivalent `server.connect(...)` call for other SDKs) — registering
      tools alone is not enough.
- [ ] `runtime-<name>` and `versioned-<name>` stages exist in `Dockerfile`.
- [ ] `lvx-<name>` exists in `docker-compose.yml` with `linux/amd64`.
- [ ] All nine AI-host configuration templates include the server (only the
      Linux variants get `--network host`, and only if needed).
- [ ] Tokens are secure inputs or environment variables, never committed.
- [ ] `docker compose build lvx-<name>` succeeds.
- [ ] The AI host starts the configured local MCP container and one tool works.
- [ ] Root and per-server documentation are updated.

## Bonus: Prompt for a New MCP Implementation

Use this when there is no existing MCP source to integrate. Replace only the
header values. The rest of the prompt is the standard implementation guide.

The header below is a Grafana example. For another MCP, replace the name,
purpose, and required environment variables. The MCP name must not match an
existing server in `servers/`; `lvx-<name>` is derived automatically for the
Compose service and image name.

```text
# ── USER FILLS THESE VARIABLES ───────────────────────────────────────────────
# MCP_NAME: <MCP_NAME>
# What it does: <one-paragraph description of tools and safety rules>
# OperatingSystem: <linux|Windows|Macos>  (the OS you'll build/test this on)
# Required token/secret environment variables: <TOKEN_NAME>, <ANOTHER_SECRET>
#   (write "none" if no secrets are needed)
# Required non-secret environment variables: <VAR_NAME=description>
#   (write "none" if no extra env vars are needed)
# Network/access prerequisite: <VPN, firewall rule, IAM role, etc. or "none">
# Existing scripts, repositories, or SDKs to reuse: <paths or URLs, or "none">
# API, CLI, or workflow operations to expose as tools: <specific operations>
# Reference documentation or examples: <URLs, paths, or ticket links>
# ── DO NOT EDIT BELOW THIS LINE ───────────────────────────────────────────────

Implement the MCP defined in the header above in this ai-mcp-docker registry.
Create `ai-mcp-docker/servers/<MCP_NAME>/`, the `runtime-<MCP_NAME>` and
`versioned-<MCP_NAME>` Docker stages, and the Docker Compose service
`lvx-<MCP_NAME>`. Do not take a separate Compose service name from the header.
Before editing, verify <MCP_NAME> does not already appear in
`ai-mcp-docker/servers/`, `docker-compose.yml`, or the AI-host configuration
templates. First inspect the existing servers, Dockerfile, docker-compose.yml,
and AI-host configuration templates; follow their established patterns.

Implement the tools described in "What it does" and the declared API, CLI, or
workflow operations. Use the listed scripts, repositories, SDKs, and reference
documentation as the source of truth; inspect them before designing tool names,
arguments, response shapes, and safety rules. Package the server as a
linux/amd64 Docker image that exposes MCP over stdio. Keep protocol responses
on stdout and logs on stderr. Add the server README and entries in the root
and servers README tables.

Critical handshake requirement: the container's entrypoint MUST actually start
the stdio transport loop, not just construct the server object and register
tools. For a Python/FastMCP server this means ending `server.py` with
`if __name__ == "__main__": mcp.run(transport="stdio")` (verify every existing
`servers/*/server.py` does this and match it). For any other language, call
that SDK's equivalent stdio-connect entrypoint (e.g. the Node MCP SDK's
`server.connect(new StdioServerTransport())`). Without this call the process
registers tools and exits/idles without ever reading stdin, and
`validate_mcp_protocol.py` will fail with a timeout that gives no obvious clue
as to the missing line.

Upgrade-checking rule: register `check_for_updates` in the new server with
`_install_version_notifier(mcp, "lvx-<MCP_NAME>")` immediately after its
`FastMCP(...)` initialization. For upgrade requests, the AI host calls GitHub's
`check_for_updates` first; GitHub returns the fresh private-registry `versions`
manifest, which the host passes to this server's notifier for comparison with
its local `BUILD_VERSION`. Do not give the new server a GitHub token.

Credentials rule: if "Required token/secret environment variables" is "none",
do NOT add any `-e TOKEN`, `env:`, or prompt-driven input blocks to any config
template or Compose service. If tokens are declared, define each as a container
environment variable and expose it as a secure, prompt-driven input in every
VS Code, Claude Desktop, and Claude CLI configuration file. Never hardcode
secrets.

Expose every non-secret environment variable declared in "Required non-secret
environment variables" to the container via the Compose service `environment`
block and each config template `env` block. If "none", omit those blocks.

Document the network/access prerequisite declared above in the server README
and in every config template comment where relevant.

Regardless of the declared OperatingSystem, update all nine per-OS host
configuration templates — macOS, Linux, and Windows variants for VS Code,
Claude Desktop, and Claude CLI — so every platform can consume the new
server. OperatingSystem only tells you which one the contributor will
build/test locally; it does not limit which templates get the new entry.
Add `--network host` only to the Linux-variant templates, and only when the
declared network/access prerequisite requires it (VPN-only internal hosts);
leave the macOS and Windows variants without it.

Do not hardcode credentials. Do not build or smoke-test the Docker Compose
service; the contributor runs the manual build and validation steps in this
guide. Show the exact files changed and any required permissions or external
prerequisites. Do not change unrelated MCP servers.
```

For a different service, change only the header values at the top of the
prompt.

## Security by design

Security is not a phase. It is a constraint baked into every tool, skill, and MCP server from the first line of code. A tool that works but isn't secure isn't done.

Two failure modes to avoid. Treating security as a pre-production hardening step, which means every prototype goes to production with vulnerabilities already in it. And treating human approval as the primary security control, which doesn't scale as agent output volume grows. One produces unsafe tools; the other produces unreviewed ones.

The five properties every AI tool in this space must have:

- **Trust the boundary.** All external data — ticket bodies, API responses, device output, user input — is untrusted until sanitized and extracted to the minimum necessary structured fields before it enters LLM context. Raw string interpolation into prompts is never acceptable.
- **Minimize exposure.** Every data field the tool touches has an explicit design-time decision: does it need to reach the LLM? be stored? appear in logs? Default is no. PII and credentials are held by the component that needs them and nothing else.
- **Validate output as untrusted.** An LLM response is an untyped string. Every tool enforces an explicit schema contract in code before acting on any field. "Return valid JSON" in a system prompt is not a contract.
- **Fail visibly.** Auth failures exit immediately. Every error type has a named handler. No bare catch-alls. Non-zero exit codes propagate to schedulers and orchestrators.
- **Review at scale.** As agent output grows, human review shrinks as a percentage of what gets checked. Automated security gates run on all agent-generated code and config. Tool and skill installations are versioned in Git and traceable to an approval.

The first four properties apply to every tool at Layer 1, from Phase 1 onward. The fifth becomes critical at Phase 3 and beyond.

**See**: ADR Build Philosophy — Validating agent output. ADR Build Philosophy — Error handling discipline.
