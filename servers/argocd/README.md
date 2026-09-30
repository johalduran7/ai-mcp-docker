# ArgoCD MCP Server

Read-only ArgoCD MCP server for the MyCompany / MyCompany ArgoCD instance
at `argocd.devops.mycompany.com`. Wraps the `argocd` CLI running
inside the container so an AI host can inspect applications, resources,
manifests, logs, diffs, projects, clusters, and repositories — plus
orchestrate the SSO login flow itself when the local session is missing
or expired.

> **THIS SERVER IS READ-ONLY.** No sync / rollback / delete / patch /
> create tools. Every specialized tool wraps a read verb, and the
> `argocd_cli` freestyle tool enforces an allow-list of read-only
> subcommands.

## Credential model

Same pattern the `lvx-aws` server uses for `~/.aws` / `~/.awsume`: the
container mounts the host's `~/.config/argocd` directory as a live
read-write volume. Any session written on the host is immediately
visible inside the container, and vice versa.

Two ways to authenticate:

1. **Host-side.** Run `argocd login argocd.devops.mycompany.com --sso --grpc-web`
   in your own terminal. The token lands in `~/.config/argocd/config`
   and the container picks it up on the next tool call. Uses the
   argocd CLI's default callback port `8085`.
2. **Through the AI host.** Call the `argocd_login` tool. The server
   starts `argocd login --sso --grpc-web --sso-port 8085` inside the
   container, captures the `Performing authorization_code flow login:
   <URL>` line, and returns the URL. The user opens it in their
   browser, completes SSO, and the OAuth callback lands on
   `http://localhost:8085/auth/callback` — which is published from the
   container by `docker run -p 8085:8085`. `argocd` writes the token
   and exits. The next tool call succeeds silently.

**Why two different ports (8085 host / 8085 container)?** So both paths
can coexist without stopping/restarting anything. If the container also
used 8085, its Docker port publish would collide with your host's own
`argocd login` and quietly steal the browser callback. Giving each side
its own port means: run `argocd login` on the host any time you want,
call `argocd_login` from the MCP any time you want, and both write to
the same shared `~/.config/argocd/config`. The other MCP tools (`list_apps`,
`get_app`, ...) are pure API calls that go straight to
`argocd.devops.mycompany.com` and never touch either callback port.

**Auto-refresh on expired session.** You don't have to call
`argocd_login` yourself when a session expires. Every tool detects the
"not logged in / token expired" error transparently, kicks off a fresh
SSO login in the background, and returns the `authorize_url` inside the
SAME response. The AI host surfaces the URL to the user, the user
clicks + signs in, and then re-runs the ORIGINAL request — no
intermediate "do you want me to log you in?" hop. Mirrors the way
`lvx-aws` auto-refreshes awsume sessions.

The default ArgoCD server URL is `argocd.devops.mycompany.com`.
Override it per session with `-e ARGOCD_SERVER=<hostname>` on the
`docker run` command.

## Tools

| Tool | What it does |
|------|--------------|
| `argocd_context` | Report which ArgoCD server this MCP is pointed at, the config path, and the SSO callback port. Handy first call. |
| `argocd_status` | Check whether the shared config has a valid session. Returns `{logged_in: true, username, ...}` or `{logged_in: false, reason: "NOT_LOGGED_IN"}`. |
| `argocd_login` | Start the SSO login flow inside the container and return the URL for the user to click. Idempotent — skips the flow if `argocd_status` already reports a valid session (`force=true` to override). |
| `argocd_login_status` | Report on the in-flight `argocd_login` subprocess: `idle` / `pending` / `succeeded` / `failed`. |
| `list_apps` | `argocd app list -o json` with optional filters (`project`, `selector`, `cluster`, `namespace`, `search`). |
| `get_app` | `argocd app get <name> -o json`. Full application state trimmed to the fields the AI usually needs: sources, destination, sync + health, latest operation, resource summary. |
| `get_app_manifests` | `argocd app manifests <name>` (optional `revision`). Returns the rendered YAML/JSON stream ArgoCD would apply. |
| `get_app_resources` | `argocd app resources <name>` — clean resource tree. |
| `get_app_history` | `argocd app history <name> -o json` — prior sync revisions. |
| `get_app_logs` | `argocd app logs <name>` with `container` / `kind` / `group` / `resource_name` / `resource_namespace` / `since_seconds` / `tail`. `follow=true` is ignored (MCP tool calls are one-shot). |
| `app_diff` | `argocd app diff <name>` — diff between live cluster state and target manifests. |
| `list_clusters` | `argocd cluster list -o json`. |
| `list_projects` | `argocd proj list -o json`. |
| `list_repos` | `argocd repo list -o json`. |
| `argocd_cli` | **Freestyle** read-only CLI passthrough. Enforces an allow-list of read verbs (`app get`, `app list`, `app manifests`, `cluster list`, `proj get`, `version`, ...). Refuses write verbs (`app sync`, `app rollback`, `app delete`, `cluster add`, `login`, `logout`, ...) with `error="WRITE_COMMAND_REFUSED"`. Shell metacharacters (`\|`, `;`, `` ` ``, `$()`, `&&`) are rejected. |
| `check_for_updates` | On-demand release check for this image (via shared `version_notifier`). |

Every argocd call is issued with `--grpc-web --server $ARGOCD_SERVER`
automatically — you never pass them yourself.

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `ARGOCD_SERVER` | `argocd.devops.mycompany.com` | Target ArgoCD instance. Override per session on `docker run`. |
| `ARGOCD_CONFIG_DIR` | `/root/.config/argocd` | Where the shared config lives inside the container. Must match the volume mount. |
| `ARGOCD_SSO_PORT` | `8085` | Local OAuth2 callback port during `argocd login --sso` **inside the container**. Deliberately different from argocd's own default `8085` so a container login never collides with a host-terminal login. Must match the `-p <port>:<port>` published on `docker run`. |
| `ARGOCD_LOGIN_TIMEOUT` | `180` | Seconds to allow the user to complete SSO in their browser. |

## Docker run

The recommended invocation mirrors the aws / opensearch servers, with
the extra port publish for the SSO callback:

```bash
docker run --rm -i --platform linux/amd64 \
  -p 8085:8085 \
  -v "$HOME/.config/argocd:/root/.config/argocd:rw" \
  -e ARGOCD_SERVER=argocd.devops.mycompany.com \
  ghcr.io/johalduran7/ai-mcp-docker/lvx-argocd:latest \
  python /app/servers/argocd/server.py
```

To point at a different ArgoCD instance for a specific session, just
override the env var:

```bash
docker run --rm -i --platform linux/amd64 \
  -p 8085:8085 \
  -v "$HOME/.config/argocd:/root/.config/argocd:rw" \
  -e ARGOCD_SERVER=argocd.dev.internal.example.com \
  ghcr.io/johalduran7/ai-mcp-docker/lvx-argocd:latest \
  python /app/servers/argocd/server.py
```

## Example prompts

### Login / status

```
Am I logged into ArgoCD?
  → argocd_status()
```
```
Start an ArgoCD SSO login and send me the URL
  → argocd_login()
  # Presents authorize_url to click. Container's port 8085 receives the callback.
```
```
Check if my ArgoCD login finished
  → argocd_login_status()
  → argocd_status()
```

### App inspection

```
Show me the ArgoCD app apps-outbound-outbound-preagent-u2lo
  → get_app(name="apps-outbound-outbound-preagent-u2lo")
```
```
List all apps in the outbound project that are OutOfSync
  → list_apps(project="outbound")
  # Filter sync_status="OutOfSync" in the response.
```
```
Show the rendered manifests for apps-inbound-inbound-preagent-u1 at tag 27.0.51
  → get_app_manifests(name="apps-inbound-inbound-preagent-u1", revision="27.0.51")
```
```
Grab the last 500 lines of logs for the pods in apps-outbound-outbound-preagent-u2lo
  → get_app_logs(name="apps-outbound-outbound-preagent-u2lo", tail=500)
```
```
Diff the live state vs. targeted manifests for apps-outbound-outbound-preagent-u2lo
  → app_diff(name="apps-outbound-outbound-preagent-u2lo")
```

### Discovery

```
List every cluster registered with ArgoCD
  → list_clusters()
```
```
List every ArgoCD project
  → list_projects()
```
```
Which repos does ArgoCD have configured?
  → list_repos()
```

### Freestyle CLI

```
Run `argocd version --client`
  → argocd_cli(command="version --client")
```
```
Get the outbound project as YAML
  → argocd_cli(command="proj get outbound -o yaml")
```
```
argocd_cli(command="app sync foo")  # → error="WRITE_COMMAND_REFUSED"
```
