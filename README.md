# MCP Docker Registry

This repo hosts eight MCP servers distributed as Docker images on GitHub Container
Registry (GHCR). All eight use stdio transport. You do **not** need to clone this
repo to use them as a consumer.

| Server | Image | Author |
|--------|-------|--------|
| [jira](servers/jira/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-jira:latest` | [John Duran](https://github.com/jduranlv) |
| [confluence](servers/confluence/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-confluence:latest` | [John Duran](https://github.com/jduranlv) |
| [aws](servers/aws/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-aws:latest` | [John Duran](https://github.com/jduranlv) |
| [gitlab](servers/gitlab/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-gitlab:latest` | [John Duran](https://github.com/jduranlv) |
| [github](servers/github/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-github:latest` | [John Duran](https://github.com/jduranlv) |
| [grafana](servers/grafana/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-grafana:latest` | [John Duran](https://github.com/jduranlv) |
| [opensearch](servers/opensearch/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-opensearch:latest` | [John Duran](https://github.com/jduranlv) |
| [argocd](servers/argocd/README.md) | `ghcr.io/johalduran7/ai-mcp-docker/lvx-argocd:latest` | [John Duran](https://github.com/jduranlv) |

Together, these servers cover the DevOps deploy workflow end to end: find
what's approved (Jira) → drive the config change (GitLab) → verify and refresh
the running fleet (AWS) → inspect source code at a specific deployed version
when investigating an issue (GitHub) → query Loki logs per env for anything
CloudWatch doesn't cover (Grafana) → drill into OpenSearch indices / snapshots
/ cluster health when a data-plane question comes up (OpenSearch) → inspect
ArgoCD applications, manifests, and sync state on the MyCompany clusters (ArgoCD).

**Per-server tool tables, configuration knobs, and example prompts live in
each server's README.** This root README covers only the
overview, installation, and developer workflow.

---

## For Users (consuming the MCP servers)

No repo checkout needed. You only need Docker and an AWS profile with awsume
installed.

### Step 1 — Pull the images

Log in first with a **classic** GitHub
Personal Access Token (fine-grained tokens do not grant `read:packages`). If
you don't have one yet:

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens).
2. Generate a **classic** token, check all permissions, expiration **Never**.
3. Click **Configure SSO** → **Authorize** the `johalduran7` org.

```bash
echo <YOUR_CLASSIC_GITHUB_TOKEN> | docker login ghcr.io -u <Your_Github_User> --password-stdin
```

Then pull:

```bash
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-jira:latest
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-confluence:latest
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-aws:latest
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-gitlab:latest
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-github:latest
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-grafana:latest
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-opensearch:latest
docker pull ghcr.io/johalduran7/ai-mcp-docker/lvx-argocd:latest
```

### Step 2 — Wire your AI host

Pick your AI host and click the matching template. Windows users go through
WSL2, so those templates run `docker` *inside* WSL Ubuntu via
`wsl.exe bash -lc ...` — the same pattern for every server.

- **Claude Desktop** — [macOS](config_templates/claude-desktop/claude_desktop_config.macos.example.json) · [Linux](config_templates/claude-desktop/claude_desktop_config.linux.example.json) · [Windows (WSL2)](config_templates/claude-desktop/claude_desktop_config.windows.example.json)
- **VS Code Copilot** — [macOS](config_templates/vscode/mcp.json.macos.example) · [Linux](config_templates/vscode/mcp.json.linux.example) · [Windows (WSL2)](config_templates/vscode/mcp.json.windows.example)
- **Claude CLI** — [macOS](config_templates/claude-cli/claude_cli_mcp_add.macos.example.md) · [Linux](config_templates/claude-cli/claude_cli_mcp_add.linux.example.md) · [Windows (WSL2)](config_templates/claude-cli/claude_cli_mcp_add.windows.example.md)

> **Windows prerequisite:** install Docker Desktop for Windows with the WSL2
> backend. Docker installed only inside WSL Ubuntu (`apt install docker.io`)
> is NOT visible to Claude Desktop / VS Code on Windows — you need Docker
> Desktop so the `docker` binary is on the Windows PATH and shares the engine
> with your WSL distro.

**Claude Desktop.** Copy the matching JSON template into
`claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json` (use the `windows`
  template)

Fill in the tokens shown as placeholders, then full-quit-restart Claude
Desktop.

**VS Code Copilot.** Copy the matching template to `.vscode/mcp.json` in
your workspace. Tokens are requested securely via VS Code input prompts
(`${input:jira_token}` / `${input:gitlab_token}` / etc.) — they're never
hardcoded in the file. Open Command Palette → `MCP: List Servers` and start
the servers you need.

**Claude CLI.** Both `.md` files contain ready-to-run `claude mcp add-json`
commands for every server.

### Step 3 — Authenticate your AWS profile with awsume

The `aws` and `opensearch` servers mount your host's `~/.aws` and `~/.awsume`
as live read-write volumes, so any session you create on the host is
immediately visible inside the container and vice versa. Authenticate in
whichever way is convenient:

**Option A — from your terminal:**

```bash
awsume <your-profile> -o <your-profile>-session
```

**Option B — through the AI host:** ask it to refresh the session; it will
prompt you for the MFA code only if awsume actually needs one.

Switching profiles works the same way — run
`awsume <other-profile> -o <other-profile>-session` at any time and the AI
host picks it up immediately without a restart.

> **Troubleshooting — only if you hit permission errors.** In normal use
> you don't need to touch anything here. If awsume-inside-the-container ever
> fails with permission-denied on `~/.aws/credentials` or `~/.awsume/`,
> verify on your host that both paths are owned by you and writable:
>
> ```bash
> ls -lrth ~/.aws/credentials ~/.aws/config
> ls -ld ~/.aws ~/.awsume
> sudo chown -R "$USER" ~/.aws ~/.awsume
> chmod u+rwX -R ~/.aws ~/.awsume
> ```

### Step 4 — Restart your AI host

Fully quit and reopen Claude Desktop (or reload VS Code) so it picks up the
new MCP server config.

### Step 5 — Try it out

Every server exposes prompt-driven tools. See each server's README for
category-organized example prompts:

- [servers/jira/README.md](servers/jira/README.md)
- [servers/confluence/README.md](servers/confluence/README.md)
- [servers/aws/README.md](servers/aws/README.md)
- [servers/gitlab/README.md](servers/gitlab/README.md)
- [servers/github/README.md](servers/github/README.md)
- [servers/grafana/README.md](servers/grafana/README.md)
- [servers/opensearch/README.md](servers/opensearch/README.md)
- [servers/argocd/README.md](servers/argocd/README.md)

### Check for MCP server updates

Every server exposes a `check_for_updates` tool. Ask your AI host:

```
Check for updates on the jira MCP server
```

If an update is available, the tool tells you the exact `docker pull`
command and to restart your AI host.

### Reset your local container setup

If the servers start misbehaving (stale image, half-broken container, "tool
not found" after an update), wipe everything MCP-related and let the AI host
pull fresh images on next launch. This only touches images/containers whose
repository matches `johalduran7/lvx-*` — nothing else on your Docker host is
affected.

```bash
docker ps -a --format '{{.ID}} {{.Image}}' | grep -iE 'johalduran7.*mcp' | awk '{print $1}' | xargs -r docker rm -f; \
docker image ls --format '{{.Repository}} {{.ID}}' | grep -iE 'johalduran7.*mcp' | awk '{print $2}' | sort -u | xargs -r docker rmi -f
```

After running the command above, re-pull the images (Step 1) and restart
your AI host.

---

## For Developers (adding or modifying MCP servers)

### Project layout

```
servers/jira/         Jira MCP server source + README
servers/confluence/   Confluence (mycompany + mycompany) MCP server source + README
servers/aws/          AWS MCP server source + README
servers/gitlab/       GitLab MCP server source + README
servers/github/       GitHub MCP server source + README
servers/grafana/      Grafana / Loki MCP server source + README
servers/opensearch/   OpenSearch MCP server source + README
servers/argocd/       ArgoCD MCP server source + README
Dockerfile            Shared image definition (linux/amd64, per-server runtime + versioned stages)
docker-compose.yml    Build / run / push configuration
resources/            Shared assets (version_notifier.py, screenshots)
.env.example          Optional local overrides for Compose (image naming, host paths)
.dockerignore         Keeps local .env files out of the Docker build context
```

### Step 1 — Optional local `.env` for Compose

Docker Compose automatically reads a `.env` file in the same directory to
expand `${VAR}` placeholders. All variables have defaults, so this file is
optional.

```bash
cp .env.example .env
# edit only what you need to override
```

Overridable variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `GHCR_OWNER` | `johalduran7` | GHCR image namespace |
| `GHCR_REPO` | `ai-mcp-docker` | Repo path segment under `GHCR_OWNER`, so packages can't collide with a same-named package from another repo in the org |
| `GHCR_TAG` | `latest` | Image tag |
| `HOST_AWS_DIR` | `$HOME/.aws` | Host path mounted into container |
| `HOST_AWSUME_DIR` | `$HOME/.awsume` | Host awsume cache mounted into container |
| `AWS_DEFAULT_REGION` | `us-east-1` | Fallback region for boto3 |
| `GITLAB_ENDPOINT` | `https://vcs.build.mycompany.net/eng` | GitLab host + group path for `<component>-config` repos |
| `GITLAB_TOKEN` | _(empty)_ | Injected by the AI host MCP config; never hardcode |
| `GITHUB_ORG` | `myorgname2` | GitHub organization scanned by `lvx-github` |
| `GITHUB_BASE_URL` | `https://api.github.com` | GitHub API base URL |
| `GITHUB_TOKEN` | _(empty)_ | Injected by the AI host MCP config; read-only scope is enough |
| `ARGOCD_SERVER` | `argocd.devops.mycompany.com` | Target ArgoCD instance for `lvx-argocd` |
| `ARGOCD_SSO_PORT` | `8085` | OAuth2 callback port published from the `lvx-argocd` container. Kept off argocd's default `8085` so container-side `argocd_login` never collides with a host-terminal `argocd login`. |
| `HOST_ARGOCD_DIR` | `$HOME/.config/argocd` | Host argocd config dir mounted into the argocd container |

### Step 2 — Build and test locally

```bash
# Run this off VPN if your corporate network blocks Docker Hub
docker compose build

# Smoke-test each server (Ctrl-C to exit; the servers speak stdio MCP)
docker compose run --rm -T lvx-jira
docker compose run --rm -T lvx-confluence   # needs at least one confluence token
docker compose run --rm -T lvx-aws
docker compose run --rm -T lvx-gitlab       # needs corporate VPN
docker compose run --rm -T lvx-github       # needs internet + GITHUB_TOKEN
docker compose run --rm -T lvx-grafana      # needs corporate VPN + LDAP creds
docker compose run --rm -T lvx-opensearch   # needs awsume session + VPN for the target env
docker compose run --rm -T lvx-argocd       # needs corporate VPN + `argocd login` session
```

### Step 3 — Release to GHCR

**Publishing to `ghcr.io/johalduran7/ai-mcp-docker/lvx-*` only happens through CI, from
`main`, after merge.** Nobody pushes packages by hand. See *Continuous
Integration* below for the full pipeline.

For a one-off manual push:

```bash
echo <ghcr-pat> | docker login ghcr.io -u <your-username> --password-stdin
docker compose build
docker compose push
```

### Adding a new MCP server

1. Create `servers/<name>/` with:
   - `server.py` — stdio MCP server. Import shared version notifier:
     ```python
     from version_notifier import install as _install_version_notifier
     _install_version_notifier(mcp, "lvx-<name>")
     ```
   - `requirements.txt`
   - `run_server.sh`
   - `README.md` — description, tools table, configuration, example prompts.
2. Add `runtime-<name>` and `versioned-<name>` stages to `Dockerfile`
   (mirror the pattern used by the other services), including a
   `COPY servers/<name>/requirements.txt` + `pip install` step in the
   `runtime-<name>` stage — each server installs its own deps
   independently, not merged into the shared base layer.
3. Add an `lvx-<name>` service to `docker-compose.yml`.
   The CI pipelines pick it up automatically.
4. Update all nine example configs — macOS, Linux, and Windows — with the new server's snippet:
   - `config_templates/claude-desktop/claude_desktop_config.macos.example.json` + `config_templates/claude-desktop/claude_desktop_config.linux.example.json` + `config_templates/claude-desktop/claude_desktop_config.windows.example.json`
   - `config_templates/vscode/mcp.json.macos.example` + `config_templates/vscode/mcp.json.linux.example` + `config_templates/vscode/mcp.json.windows.example`
   - `config_templates/claude-cli/claude_cli_mcp_add.macos.example.md` + `config_templates/claude-cli/claude_cli_mcp_add.linux.example.md` + `config_templates/claude-cli/claude_cli_mcp_add.windows.example.md`
5. Add the new server row to the table at the top of this README and to
   [servers/README.md](servers/README.md).

Steps 2–5 aren't optional courtesy — the `validate` pipeline (below) fails
the PR if any of them are missed.

---

## Continuous Integration

Two pipelines, two different jobs. Nobody pushes packages by hand anymore.

### 1. `validate` — mandatory check on every branch / PR

**[.github/workflows/validate.yml](.github/workflows/validate.yml)** runs on
every push to any non-`main` branch and on every PR targeting `main`. Set it
as a required status check in branch protection so no PR can merge without
passing it. It fails the build if a commit:

- Breaks YAML, JSON, or Python syntax anywhere in the repo.
- Adds/edits a server folder missing one of `server.py`, `requirements.txt`,
  `run_server.sh`, `README.md`.
- Registers a duplicate MCP name (two servers passing the same name to
  `_install_version_notifier(...)` — not tied to any particular MCP SDK).
- Leaves `docker-compose.yml` out of sync with `servers/*/` (missing
  service, stale service, or wrong image pattern).
- Leaves `Dockerfile` missing a `runtime-<name>` / `versioned-<name>` stage
  for the server.
- Leaves this README or [servers/README.md](servers/README.md) without a
  row for the server.
- Leaves any `config_templates/{claude-desktop,vscode,claude-cli}/*`
  template without an entry for the server — every AI host's config must
  stay in sync for every MCP, every time, so nobody's working setup breaks
  silently.

All of this logic lives in one script,
[.github/scripts/validate_repo.py](.github/scripts/validate_repo.py), so the
rules are auditable and testable locally:

```bash
pip install pyyaml
python3 .github/scripts/validate_repo.py
```

### 2. `publish` — automatic release on `main`, post-merge only

**[.github/workflows/publish.yml](.github/workflows/publish.yml)** runs only
on `push` to `main` (i.e. after a PR merges — direct pushes to `main` should
be disabled in branch protection). For each service it:

1. Hashes that service's source (`servers/<name>/**` — including that
   server's own `requirements.txt`, installed independently per Dockerfile
   stage — the shared `version_notifier.py`, `Dockerfile`/`.dockerignore`,
   and that service's resolved `docker-compose.yml` block) using
   [.github/scripts/detect_changed_services.py](.github/scripts/detect_changed_services.py).
2. Compares that hash against `versions/mcp-version.json`. Unchanged
   source → skipped entirely (no rebuild, no republish).
3. For everything that changed: builds the `versioned-<name>` Dockerfile
   stage and pushes `ghcr.io/johalduran7/ai-mcp-docker/lvx-<name>:latest` using the
   workflow's own `GITHUB_TOKEN` — no personal access token, no manual
   `docker login` required.
4. Commits the updated `versions/mcp-version.json` to the `versions` branch (this is
   also the file `check_for_updates` reads from — see
   [resources/version_notifier.py](resources/version_notifier.py)).

Because everything is driven by a source-content hash instead of a build
timestamp, a PR that only touches, say, `servers/jira/`, produces exactly
one new image — every other server is skipped automatically.
