# Claude CLI MCP add-json examples — Windows (WSL2)

Register the MyCompany MCP servers with Claude CLI on Windows via WSL2.

> IMPORTANT: Claude CLI must be **installed and run from inside WSL**
> (e.g. an Ubuntu terminal), NOT from PowerShell/cmd.exe on the Windows
> side. Every entry below spawns `bash -lc "docker run ..."` directly —
> there's no `wsl.exe` re-entry, because Claude CLI is already running
> inside the WSL Linux environment. If Claude CLI is run natively on
> Windows instead, spawning `bash` here will fail with `ENOENT` because
> there's no `bash.exe` on the Windows side.

## Prerequisites

- Docker Desktop for Windows with WSL2 integration enabled for your distro.
- Claude CLI installed inside WSL (e.g. via `npm install -g` from your
  Ubuntu/WSL terminal) and run from a WSL shell — not the Windows `PATH`.

## Quoting note

These commands run inside a WSL bash shell, so use single-quoted JSON
blobs as shown (bash preserves everything inside single quotes
literally). Paste them into your WSL terminal, not PowerShell or cmd.exe.

## 1) Add Jira MCP server (dual-site: mycompany + mycompany)

> Configure at least one site. `JIRA_TOKEN` is a MyCompany Data Center PAT
> (Bearer). `JIRA_CLOUD_TOKEN` is an Atlassian Cloud API token — classic
> ones require HTTP Basic auth, so set `JIRA_CLOUD_EMAIL` too.

```bash
claude mcp add-json -s user lvx-jira '{
  "command": "bash",
  "env": {
    "JIRA_BASE_URL": "https://jira.mycompany.com",
    "JIRA_TOKEN": "paste-your-jira-token-here",
    "JIRA_CLOUD_BASE_URL": "https://mycompany-prod.atlassian.net",
    "JIRA_CLOUD_EMAIL": "your_email@mycompany.com",
    "JIRA_CLOUD_TOKEN": "paste-your-mycompany-atlassian-api-token-here"
  },
  "args": [
    "-lc",
    "docker run --rm -i --platform linux/amd64 -e JIRA_BASE_URL -e JIRA_TOKEN -e JIRA_CLOUD_BASE_URL -e JIRA_CLOUD_EMAIL -e JIRA_CLOUD_TOKEN ghcr.io/johalduran7/ai-mcp-docker/lvx-jira:latest"
  ]
}'
```

## 2) Add Confluence MCP server (dual-site: mycompany + mycompany)

> Configure at least one site. `CONFLUENCE_CLOUD_TOKEN` is an Atlassian
> Cloud API token — classic ones require HTTP Basic auth, so set
> `CONFLUENCE_CLOUD_EMAIL` too. `CONFLUENCE_CLOUD_TOKEN` is a
> Confluence Data Center PAT (Bearer).

```bash
claude mcp add-json -s user lvx-confluence '{
  "command": "bash",
  "env": {
    "CONFLUENCE_CLOUD_BASE_URL": "https://mycompany-prod.atlassian.net/wiki",
    "CONFLUENCE_CLOUD_EMAIL": "your_email@mycompany.com",
    "CONFLUENCE_CLOUD_TOKEN": "paste-your-mycompany-atlassian-api-token-here",
    "CONFLUENCE_ONPREM_BASE_URL": "https://library.mycompany.com",
    "CONFLUENCE_ONPREM_TOKEN": "paste-your-mycompany-confluence-pat-here"
  },
  "args": [
    "-lc",
    "docker run --rm -i --platform linux/amd64 -e CONFLUENCE_CLOUD_BASE_URL -e CONFLUENCE_CLOUD_EMAIL -e CONFLUENCE_CLOUD_TOKEN -e CONFLUENCE_ONPREM_BASE_URL -e CONFLUENCE_ONPREM_TOKEN ghcr.io/johalduran7/ai-mcp-docker/lvx-confluence:latest python /app/servers/confluence/server.py"
  ]
}'
```

## 3) Add AWS MCP server

```bash
claude mcp add-json -s user lvx-aws '{
  "command": "bash",
  "args": [
    "-lc",
    "docker run --rm -i --platform linux/amd64 -v \"$HOME/.aws:/root/.aws:rw\" -v \"$HOME/.awsume:/root/.awsume:rw\" -e AWS_CONFIG_FILE=/root/.aws/config -e AWS_SHARED_CREDENTIALS_FILE=/root/.aws/credentials ghcr.io/johalduran7/ai-mcp-docker/lvx-aws:latest python /app/servers/aws/server.py"
  ]
}'
```

`$HOME` expands inside WSL, so no username to hardcode — works whether
you're `root` in WSL (`/root/.aws`) or a regular user (`/home/<you>/.aws`).

## 4) Add GitLab MCP server

```bash
claude mcp add-json -s user lvx-gitlab '{
  "command": "bash",
  "env": {
    "GITLAB_ENDPOINT": "https://vcs.build.mycompany.net/eng",
    "GITLAB_TOKEN": "paste-your-gitlab-token-here"
  },
  "args": [
    "-lc",
    "docker run --rm -i --platform linux/amd64 -e GITLAB_ENDPOINT -e GITLAB_TOKEN ghcr.io/johalduran7/ai-mcp-docker/lvx-gitlab:latest python /app/servers/gitlab/server.py"
  ]
}'
```

## 5) Add GitHub MCP server (read-only)

> Strictly read-only. Provide a PAT with `contents:read` /
> `metadata:read` on the `myorgname2` organization.

```bash
claude mcp add-json -s user lvx-github '{
  "command": "bash",
  "env": {
    "GITHUB_ORG": "myorgname2",
    "GITHUB_TOKEN": "paste-your-github-pat-here"
  },
  "args": [
    "-lc",
    "docker run --rm -i --platform linux/amd64 -e GITHUB_ORG -e GITHUB_TOKEN ghcr.io/johalduran7/ai-mcp-docker/lvx-github:latest python /app/servers/github/server.py"
  ]
}'
```

## 6) Add Grafana MCP server (read-only)

> Strictly read-only. Provide your LDAP credentials. `{env}` in the URL
> template is substituted per query (e.g. `na3` →
> `logging-ui.service.na3.mycompany.net`). Two providers are supported
> per-call (`provider="lvx"` default, or `provider="mycompany"` for MyCompany org's
> own Grafana at the hardcoded `mon-*` hosts, which needs
> `OKTA_USERNAME`/`OKTA_PASSWORD` below and the Forticlient VPN instead of
> Cisco).

```bash
claude mcp add-json -s user lvx-grafana '{
  "command": "bash",
  "env": {
    "GRAFANA_URL_TEMPLATE": "http://logging-ui.service.{env}.mycompany.net:8080",
    "GRAFANA_USERNAME": "your-ldap-username",
    "GRAFANA_PASSWORD": "your-ldap-password",
    "OKTA_DOMAIN": "mycompany.okta.com",
    "OKTA_USERNAME": "your-okta-username",
    "OKTA_PASSWORD": "your-okta-password"
  },
  "args": [
    "-lc",
    "docker run --rm -i --platform linux/amd64 -e GRAFANA_URL_TEMPLATE -e GRAFANA_USERNAME -e GRAFANA_PASSWORD -e OKTA_DOMAIN -e OKTA_USERNAME -e OKTA_PASSWORD ghcr.io/johalduran7/ai-mcp-docker/lvx-grafana:latest python /app/servers/grafana/server.py"
  ]
}'
```

## 7) Add OpenSearch MCP server (read-only)

> Strictly read-only. Shares the lvx-aws credential model — AWS profiles
> from `~/.aws/config`, awsume refresh handled internally, and the
> container mounts `~/.aws` and `~/.awsume` from WSL. Provide `env` and
> `profile_name` on each tool call.

```bash
claude mcp add-json -s user lvx-opensearch '{
  "command": "bash",
  "args": [
    "-lc",
    "docker run --rm -i --platform linux/amd64 -v \"$HOME/.aws:/root/.aws:rw\" -v \"$HOME/.awsume:/root/.awsume:rw\" -e AWS_CONFIG_FILE=/root/.aws/config -e AWS_SHARED_CREDENTIALS_FILE=/root/.aws/credentials ghcr.io/johalduran7/ai-mcp-docker/lvx-opensearch:latest python /app/servers/opensearch/server.py"
  ]
}'
```

## 8) Add ArgoCD MCP server (read-only)

> Strictly read-only. A shared "anchor" container (`argocd-mcp-8085`) is
> created once inside WSL and reused by every MCP session — needed
> because the SSO callback always lands on port `8085`. The container
> mounts `~/.config/argocd` from WSL, so any session written by
> `argocd login` (either on the host or via the `argocd_login` MCP tool)
> is shared with the container. Default target is
> `argocd.devops.mycompany.com` — override via
> `-e ARGOCD_SERVER=<host>`.

```bash
claude mcp add-json -s user lvx-argocd '{
  "command": "bash",
  "args": [
    "-lc",
    "docker network create argocd-net >/dev/null 2>&1; IMG=ghcr.io/johalduran7/ai-mcp-docker/lvx-argocd:latest; VOL=\"$HOME/.config/argocd:/root/.config/argocd:rw\"; IMG_ID=$(docker image inspect -f '{{.Id}}' \"$IMG\" 2>/dev/null | sed 's/^sha256://' | cut -c1-8); ANCHOR=\"argocd-mcp-8085-$IMG_ID\"; RUNNING=$(docker inspect -f '{{.State.Running}}' \"$ANCHOR\" 2>/dev/null); if [ \"$RUNNING\" != \"true\" ]; then for old in $(docker ps -aq --filter \"publish=8085\" 2>/dev/null); do oldname=$(docker inspect -f '{{.Name}}' \"$old\" 2>/dev/null | sed 's#^/##'); [ \"$oldname\" != \"$ANCHOR\" ] && docker rm -f \"$old\" >/dev/null 2>&1; done; docker run -d --rm --platform linux/amd64 --name \"$ANCHOR\" --network argocd-net -p 8085:8085 -v \"$VOL\" -e ARGOCD_SERVER=argocd.devops.mycompany.com -e ARGOCD_SSO_PORT=8085 \"$IMG\" sleep infinity >/dev/null 2>&1 || true; fi; TRIES=0; while [ \"$(docker inspect -f '{{.State.Running}}' \"$ANCHOR\" 2>/dev/null)\" != \"true\" ] && [ $TRIES -lt 50 ]; do sleep 0.1; TRIES=$((TRIES+1)); done; exec docker exec -i \"$ANCHOR\" python /app/servers/argocd/server.py"
  ]
}'
```

## 9) Verify

```bash
claude mcp list
```

> Every command above uses `-s user` scope, so servers are available in every
> project directory, not just the one you're in when you run the command. If
> you previously registered a server without `-s user` (the default is local
> scope, tied to the current directory), remove the local-scoped entry first,
> then re-add at user scope:
>
> ```bash
> claude mcp remove lvx-aws -s local
> claude mcp add-json -s user lvx-aws '{ ... }'
> ```
>
> To update a server's config, remove it first with `claude mcp remove <name> -s user`
> (e.g. `claude mcp remove lvx-argocd -s user`), then re-run the `add-json` command
> above with your changes.

If your images are private, log in first from a WSL shell:

```bash
echo <github_pat> | docker login ghcr.io -u <github_username> --password-stdin
```
