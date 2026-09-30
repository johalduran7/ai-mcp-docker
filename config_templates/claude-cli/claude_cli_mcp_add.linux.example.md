# Claude CLI MCP add-json examples — Linux

Use these commands to register the published GHCR MCP servers in Claude CLI.

> **Native Linux only** — not WSL. Every `docker run` below carries
> `--network host`: Docker's default bridge network can't see your host's
> VPN routes/DNS, so internal-only hosts (GitLab, Grafana, ArgoCD)
> are unreachable from inside the container without it. Public endpoints
> (GitHub, Jira, Confluence) work either way — host networking doesn't hurt
> them. Running inside WSL? Use `claude_cli_mcp_add.macos.example.md`
> instead — WSL already reaches these hosts without `--network host`.

## 1) Add Jira MCP server (dual-site: mycompany + mycompany)

> Configure at least one site. `JIRA_TOKEN` is a MyCompany Data Center PAT
> (Bearer). `JIRA_CLOUD_TOKEN` is an Atlassian Cloud API token — classic
> ones require HTTP Basic auth, so set `JIRA_CLOUD_EMAIL` too.

```bash
claude mcp add-json -s user lvx-jira '{
  "command": "docker",
  "env": {
    "JIRA_BASE_URL": "https://jira.mycompany.com",
    "JIRA_TOKEN": "paste-your-jira-token-here",
    "JIRA_CLOUD_BASE_URL": "https://mycompany-prod.atlassian.net",
    "JIRA_CLOUD_EMAIL": "your_email@mycompany.com",
    "JIRA_CLOUD_TOKEN": "paste-your-mycompany-atlassian-api-token-here"
  },
  "args": [
    "run",
    "--rm",
    "-i",
    "--network",
    "host",
    "--platform",
    "linux/amd64",
    "-e",
    "JIRA_BASE_URL",
    "-e",
    "JIRA_TOKEN",
    "-e",
    "JIRA_CLOUD_BASE_URL",
    "-e",
    "JIRA_CLOUD_EMAIL",
    "-e",
    "JIRA_CLOUD_TOKEN",
    "ghcr.io/johalduran7/ai-mcp-docker/lvx-jira:latest"
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
  "command": "docker",
  "env": {
    "CONFLUENCE_CLOUD_BASE_URL": "https://mycompany-prod.atlassian.net/wiki",
    "CONFLUENCE_CLOUD_EMAIL": "your_email@mycompany.com",
    "CONFLUENCE_CLOUD_TOKEN": "paste-your-mycompany-atlassian-api-token-here",
    "CONFLUENCE_ONPREM_BASE_URL": "https://library.mycompany.com",
    "CONFLUENCE_ONPREM_TOKEN": "paste-your-mycompany-confluence-pat-here"
  },
  "args": [
    "run",
    "--rm",
    "-i",
    "--network",
    "host",
    "--platform",
    "linux/amd64",
    "-e",
    "CONFLUENCE_CLOUD_BASE_URL",
    "-e",
    "CONFLUENCE_CLOUD_EMAIL",
    "-e",
    "CONFLUENCE_CLOUD_TOKEN",
    "-e",
    "CONFLUENCE_ONPREM_BASE_URL",
    "-e",
    "CONFLUENCE_ONPREM_TOKEN",
    "ghcr.io/johalduran7/ai-mcp-docker/lvx-confluence:latest",
    "python",
    "/app/servers/confluence/server.py"
  ]
}'
```

## 3) Add AWS MCP server

```bash
claude mcp add-json -s user lvx-aws '{
  "command": "sh",
  "args": [
    "-lc",
    "docker run --rm -i --network host --platform linux/amd64 -v \"$HOME/.aws:/root/.aws:rw\" -v \"$HOME/.awsume:/root/.awsume:rw\" -e AWS_CONFIG_FILE=/root/.aws/config -e AWS_SHARED_CREDENTIALS_FILE=/root/.aws/credentials ghcr.io/johalduran7/ai-mcp-docker/lvx-aws:latest python /app/servers/aws/server.py"
  ]
}'
```

## 4) Add GitLab MCP server

```bash
claude mcp add-json -s user lvx-gitlab '{
  "command": "docker",
  "env": {
    "GITLAB_ENDPOINT": "https://vcs.build.mycompany.net/eng",
    "GITLAB_TOKEN": "paste-your-gitlab-token-here"
  },
  "args": [
    "run",
    "--rm",
    "-i",
    "--network",
    "host",
    "--platform",
    "linux/amd64",
    "-e",
    "GITLAB_ENDPOINT",
    "-e",
    "GITLAB_TOKEN",
    "ghcr.io/johalduran7/ai-mcp-docker/lvx-gitlab:latest",
    "python",
    "/app/servers/gitlab/server.py"
  ]
}'
```

## 5) Add GitHub MCP server (read-only)

> This server is strictly read-only — no writes to GitHub. Provide a PAT
> with `contents:read` / `metadata:read` on the `myorgname2` organization.

```bash
claude mcp add-json -s user lvx-github '{
  "command": "docker",
  "env": {
    "GITHUB_ORG": "myorgname2",
    "GITHUB_TOKEN": "paste-your-github-pat-here"
  },
  "args": [
    "run",
    "--rm",
    "-i",
    "--network",
    "host",
    "--platform",
    "linux/amd64",
    "-e",
    "GITHUB_ORG",
    "-e",
    "GITHUB_TOKEN",
    "ghcr.io/johalduran7/ai-mcp-docker/lvx-github:latest",
    "python",
    "/app/servers/github/server.py"
  ]
}'
```

## 6) Add Grafana MCP server (read-only)

> This server is strictly read-only — no writes to Grafana. Provide your
> LDAP credentials (the same ones you use to log into Grafana in the
> browser). The endpoint template resolves per env; `{env}` is substituted
> at query time (e.g. `na3` → `logging-ui.service.na3.mycompany.net`). Two
> providers are supported per-call (`provider="lvx"` default, or
> `provider="mycompany"` for MyCompany org's own Grafana at the hardcoded `mon-*`
> hosts, which needs `OKTA_USERNAME`/`OKTA_PASSWORD` below and the
> Forticlient VPN instead of Cisco).

```bash
claude mcp add-json -s user lvx-grafana '{
  "command": "docker",
  "env": {
    "GRAFANA_URL_TEMPLATE": "http://logging-ui.service.{env}.mycompany.net:8080",
    "GRAFANA_USERNAME": "your-ldap-username",
    "GRAFANA_PASSWORD": "your-ldap-password",
    "OKTA_DOMAIN": "mycompany.okta.com",
    "OKTA_USERNAME": "your-okta-username",
    "OKTA_PASSWORD": "your-okta-password"
  },
  "args": [
    "run",
    "--rm",
    "-i",
    "--network",
    "host",
    "--platform",
    "linux/amd64",
    "-e",
    "GRAFANA_URL_TEMPLATE",
    "-e",
    "GRAFANA_USERNAME",
    "-e",
    "GRAFANA_PASSWORD",
    "-e",
    "OKTA_DOMAIN",
    "-e",
    "OKTA_USERNAME",
    "-e",
    "OKTA_PASSWORD",
    "ghcr.io/johalduran7/ai-mcp-docker/lvx-grafana:latest",
    "python",
    "/app/servers/grafana/server.py"
  ]
}'
```

## 7) Add OpenSearch MCP server (read-only)

> This server is strictly read-only — no writes to OpenSearch. It shares
> the lvx-aws credential model: AWS profiles are read from `~/.aws/config`
> and awsume refreshes are handled internally (the container mounts
> `~/.aws` and `~/.awsume` from the host). Provide `env` and
> `profile_name` on each tool call — no env is hardcoded.

```bash
claude mcp add-json -s user lvx-opensearch '{
  "command": "sh",
  "args": [
    "-lc",
    "docker run --rm -i --network host --platform linux/amd64 -v \"$HOME/.aws:/root/.aws:rw\" -v \"$HOME/.awsume:/root/.awsume:rw\" -e AWS_CONFIG_FILE=/root/.aws/config -e AWS_SHARED_CREDENTIALS_FILE=/root/.aws/credentials ghcr.io/johalduran7/ai-mcp-docker/lvx-opensearch:latest python /app/servers/opensearch/server.py"
  ]
}'
```

## 8) Add ArgoCD MCP server (read-only)

> Strictly read-only. Same shared "anchor" model as the other platforms,
> but on native Linux the anchor runs with `--network host` (like every
> other server in this file) instead of a private bridge network, since
> Docker's default bridge can't see this host's VPN routes/DNS. The
> container mounts `~/.config/argocd` from the host, so any session
> written by `argocd login` (either on the host or through the
> `argocd_login` tool) is visible on both sides. Default target is
> `argocd.devops.mycompany.com` — override via
> `-e ARGOCD_SERVER=<host>`.

```bash
claude mcp add-json -s user lvx-argocd '{
  "command": "sh",
  "args": [
    "-lc",
    "IMG=ghcr.io/johalduran7/ai-mcp-docker/lvx-argocd:latest; VOL=\"$HOME/.config/argocd:/root/.config/argocd:rw\"; IMG_ID=$(docker image inspect -f '{{.Id}}' \"$IMG\" 2>/dev/null | sed 's/^sha256://' | cut -c1-8); ANCHOR=\"argocd-mcp-8085-$IMG_ID\"; RUNNING=$(docker inspect -f '{{.State.Running}}' \"$ANCHOR\" 2>/dev/null); if [ \"$RUNNING\" != \"true\" ]; then for old in $(docker ps -aq --filter \"name=^argocd-mcp-8085\" 2>/dev/null); do oldname=$(docker inspect -f '{{.Name}}' \"$old\" 2>/dev/null | sed 's#^/##'); [ \"$oldname\" != \"$ANCHOR\" ] && docker rm -f \"$old\" >/dev/null 2>&1; done; docker run -d --rm --platform linux/amd64 --name \"$ANCHOR\" --network host -v \"$VOL\" -e ARGOCD_SERVER=argocd.devops.mycompany.com -e ARGOCD_SSO_PORT=8085 \"$IMG\" sleep infinity >/dev/null 2>&1 || true; fi; TRIES=0; while [ \"$(docker inspect -f '{{.State.Running}}' \"$ANCHOR\" 2>/dev/null)\" != \"true\" ] && [ $TRIES -lt 50 ]; do sleep 0.1; TRIES=$((TRIES+1)); done; exec docker exec -i \"$ANCHOR\" python /app/servers/argocd/server.py"
  ]
}'
```

## 9) Verify and test

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

If you need to refresh AWS sessions first:

```bash
awsume myprofile -o myprofile-session
```

If your images are private, log in first:

```bash
echo <github_pat> | docker login ghcr.io -u <github_username> --password-stdin
```
