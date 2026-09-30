# AWS MCP Server

Awsume-based AWS MCP server for MyCompany envs, with first-class support for
AWS IAM Identity Center (SSO) profiles (e.g. MyCompany). Covers session
management + ECS/ASG operations + CloudWatch Logs queries + a raw `aws` CLI
escape hatch. Every tool transparently refreshes an expired session — via
**awsume** for IAM profiles or via **OIDC device-code** for SSO profiles —
and only surfaces `needs_mfa: true` (IAM) or `needs_sso_login: true` (SSO)
when user interaction is unavoidable.

## Credential model

The server supports two authentication kinds, decided **per profile** by
inspecting each profile's block in `~/.aws/config`:

| Kind | How the server detects it | How refresh works |
|------|---------------------------|-------------------|
| `iam` | No `sso_session` / `sso_start_url` keys in the profile block. | `awsume <base-profile> -o <base-profile>-session` runs inside the container. If MFA is required and no code was supplied, the tool returns `needs_mfa: true` so the AI host can prompt for one. |
| `sso` | The profile block declares `sso_session = <name>` (recommended) or a legacy `sso_start_url`. | OIDC device-code flow via `sso-oidc`. On expiry the tool returns `needs_sso_login: true` with a `verification_uri_complete` URL. The user clicks, signs in, approves. On the next call the server polls `create_token`, writes the token to `~/.aws/sso/cache/`, and silently retries the original call. |

Nothing in the code hardcodes a naming convention like `mycompany-*` or `lv-*`.
Classification is purely metadata-driven, so you can name your SSO profiles
whatever you want and the server picks them up.

### IAM profiles (existing behavior — unchanged)

- Base profiles are read from `~/.aws/config`.
- Session profiles are expected in `~/.aws/credentials`.
- Session profile naming: `<base-profile>-session`.
- The container mounts `~/.aws` and `~/.awsume` from your host, so sessions
  written on the host are immediately visible inside the container and vice
  versa.

### SSO profiles (new)

- Both `[sso-session ...]` blocks and profile blocks are parsed from
  `~/.aws/config`.
- Tokens live in `~/.aws/sso/cache/<sha1(sso_session_name)>.json` (or
  `<sha1(sso_start_url)>.json` for legacy configs), in the exact format
  botocore's `SSOTokenProvider` reads.
- **The SSO cache is fully shared between host and container** — the
  compose mount is `${HOST_AWS_DIR:-${HOME}/.aws}:/root/.aws:rw`, which
  includes the `sso/cache/` subtree. Running `aws sso login --sso-session
  <name>` on the host immediately makes the token visible inside the
  container, and any token our server writes is immediately usable by the
  host's `aws` CLI.
- **MyCompany SSO requires FortiClient VPN** to reach the Identity Center
  and device-authorization endpoints. Every SSO-related response includes
  a `vpn_warning` reminding the user to turn VPN on before clicking the
  approval link.

### Example config (MyCompany IAM + MyCompany SSO side by side)

```ini
[default]
mfa_serial = arn:aws:iam::111111111111:mfa/MyPhone
region = us-east-1
output = json

[profile lv-na3]
source_profile = default
region = us-east-1
role_arn = arn:aws:iam::222222222222:role/devops
mfa_serial = arn:aws:iam::111111111111:mfa/MyPhone
output = json

# ── MyCompany SSO ────────────────────────────────────────
[sso-session mycompany]
sso_start_url = https://identitycenter.amazonaws.com/ssoins-1234567890abcdef0
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile mycompany-na3]
sso_session = mycompany
sso_account_id = 333333333333
sso_role_name = Dept_DevOps-MyCompany
region = us-east-1

[profile mycompany-eu1]
sso_session = mycompany
sso_account_id = 444444444444
sso_role_name = Dept_DevOps-MyCompany
region = eu-central-1

[profile mycompany-tst3]
sso_session = mycompany
sso_account_id = 555555555555
sso_role_name = Dept_DevOps-MyCompany
region = us-west-2
```

### Profile resolution hierarchy

`profile_name` accepts substrings on every tool. When a substring matches
both an IAM and an SSO profile, the **IAM** profile wins by default:

| Input | Resolves to |
|-------|-------------|
| `"na3"` | `lv-na3` (IAM wins because there's exactly one IAM match) |
| `"mycompany-na3"` | `mycompany-na3` (exact match) |
| `"mycompany"` | Ambiguous → tool returns `ambiguous_profile: true` with `matches_detailed` (each entry tagged `kind`) so the AI can ask the user which to use |
| `"stg4"` | `lv-stg4` (unchanged) |

Users must spell more of the SSO profile's name to select an SSO profile
when an IAM twin exists. This preserves 100% backward compatibility with
existing MyCompany prompts.

## Tools

### Session / auth

| Tool | Applies to | What it does |
|------|-----------|--------------|
| `list_config_profiles` | both | Bare list of profile names (backward-compat). |
| `list_profiles` | both | **NEW.** Rich profile listing with `kind`, `region`, and SSO metadata. Filter with `kind="sso"` or `kind="iam"`. Use this when the user asks "which SSO profiles do I have?" or "list my MyCompany profiles". |
| `list_sso_sessions` | SSO | **NEW.** Every `[sso-session ...]` block plus which profiles bind to each, plus current cached-token status per session. |
| `awsume_refresh_session` | IAM only | Refresh a profile's credentials via awsume. Rejects SSO profiles with a pointer to `sso_login`. |
| `sso_status` | SSO | **NEW.** Read-only check of the cached token: present / valid / expires-at / seconds remaining, plus any pending device authorization. No side-effects. |
| `sso_login` | SSO | **NEW.** Start (or resume) OIDC device-code auth. Returns `verification_uri_complete` + the FortiClient VPN warning. **Two-step contract:** on a fresh call the URL is returned immediately (any `wait=true` is ignored — you can't wait on a URL the user hasn't seen). On a follow-up call — after the user has clicked and signed in — `wait=true` polls for approval and returns `logged_in: true`. In practice you rarely need `wait=true`: after showing the URL, just retry the original AWS tool and the SSO refresh path picks up the fresh session automatically. |
| `auth_status` | both | Validate active credentials via STS `GetCallerIdentity`. On expiry, auto-refreshes (awsume for IAM, device-auth for SSO). |

### Inspection

| Tool | Applies to | What it does |
|------|-----------|--------------|
| `list_ecs_clusters` | both | List ECS clusters for a profile/session. |
| `describe_env_instances` | both | Complete EC2 inventory for one env in ONE call, with ASG membership, AMI names, and full tag maps. Env tag key is discovered — no MyCompany assumption. |
| `describe_env_tasks` | both | Complete ECS/Fargate task inventory for one env in ONE call, with owning service, task-def revision, container images, and tags. |
| `describe_env_log_groups` | both | **NEW.** Generic log-group listing by arbitrary `name_prefix` / `name_contains`. No MyCompany naming assumption. |
| `get_ecs_service_status` | MyCompany IAM only | Current ECS service status for a MyCompany component. |
| `get_asg_pool_status` | MyCompany IAM only | Status of the ASG pool(s) backing a component. |
| `list_env_components` | MyCompany IAM only | Discover every MyCompany component present in AWS for a given env. |
| `bulk_get_ecs_service_status` | MyCompany IAM only | Batched per-component ECS status. |

> Tools tagged **MyCompany IAM only** refuse SSO profiles up front (with a clear
> pointer to `run_aws_cli` / `describe_env_instances` / `describe_env_tasks`
> / `query_log_groups`) because their contract depends on MyCompany naming
> (`<component>-u<ver>-service-<env>`, `<component>-<ver>-asg-<env>`,
> `/<env>/<component>_<ver>` log groups) and `lv_component_*` tags.

### CloudWatch Logs (read-only)

| Tool | Applies to | What it does |
|------|-----------|--------------|
| `list_component_log_groups` | MyCompany IAM only | Log groups under `/<env>/` with component / version filters. |
| `query_component_logs` | MyCompany IAM only | Bounded Insights query across MyCompany component log groups. |
| `bulk_query_component_logs` | MyCompany IAM only | Same DSL as `query_component_logs`, fanned out per component in parallel. |
| `query_log_groups` | both | **NEW.** Same Insights DSL (`pattern` / `error_only` / `filters` / `raw_filter` / `group_by` / `fields`) but takes an explicit `log_group_names[]` list instead of MyCompany component specs. Ideal for any account (including SSO) where log groups don't follow the MyCompany convention. |

> Log-query tools only call `logs:DescribeLogGroups`, `logs:StartQuery`,
> `logs:GetQueryResults`, and `logs:StopQuery`. No write actions on
> CloudWatch Logs are ever invoked. The Insights query is bounded (max 50
> log groups per call, hard-capped 10,000 rows, wait budget max 120s).

### Fleet operations (write)

| Tool | Applies to | What it does |
|------|-----------|--------------|
| `find_refresh_targets` | MyCompany IAM only | Discover the ECS services or ASG pools that a refresh would touch. |
| `refresh_targets` | MyCompany IAM only | Refresh the confirmed ECS services / ASG pools in parallel within a single env. |

> The refresh whitelist is `au1, eu1, perf1, stg4, tst2, tst3, uk2, uk3`. Anything
> else is rejected at the tool boundary.

### Freestyle escape hatch

| Tool | Applies to | What it does |
|------|-----------|--------------|
| `run_aws_cli` | both | Runs a raw `aws …` command against one or more profiles in parallel. Read-only vs mutating classification, `confirmed_by_user=True` gate for mutations, `--profile` / `--region` injected automatically. Works transparently for SSO profiles — on expiry the tool returns the same `needs_sso_login` payload. |
| `check_for_updates` | — | On-demand release check for this image. |

## Configuration

| Env var | Purpose |
|---------|---------|
| `AWS_CONFIG_FILE` | Path inside the container (default `/root/.aws/config`). |
| `AWS_SHARED_CREDENTIALS_FILE` | Path inside the container (default `/root/.aws/credentials`). |
| `AWS_DEFAULT_REGION` | Fallback region when the profile has none. |
| `AWSUME_BIN` | Override the awsume binary path (default `awsume`). |

## Example prompts

### SSO / MyCompany (new)

**Discovery**

```
Which SSO profiles do I have?
  → list_profiles(kind="sso")
```
```
List my MyCompany profiles and their accounts
  → list_profiles(kind="sso")
```
```
Which SSO sessions am I set up for?
  → list_sso_sessions()
```
```
Is my MyCompany token still valid?
  → sso_status(sso_session="mycompany")
```

**Login**

```
Log me into MyCompany SSO
  → sso_login(sso_session="mycompany")     # NOTE: no wait=true on first call
  ← returns verification_uri_complete + FortiClient VPN warning IMMEDIATELY.
    Show the URL to the user. When they say they've clicked and approved,
    just retry the original AWS tool — the server picks up the fresh
    session automatically.
```
```
Refresh SSO for mycompany-tst3
  → sso_login(profile_name="mycompany-tst3")
  ← if already valid: already_logged_in: true
    otherwise: needs_sso_login: true with the URL to click
```
```
(Optional) After showing the URL and getting user confirmation, block
until the token lands so the next AWS call doesn't need retries:
  → sso_login(profile_name="mycompany-tst3", wait=true, max_wait_seconds=120)
  ← polls create_token every ~5s until approval → logged_in: true
```

**Using SSO profiles with normal tools**

```
Get caller identity on mycompany-au1
  → auth_status(profile_name="mycompany-au1")
  ← on first use, may return needs_sso_login: true with a URL. Click, sign
    in, approve, then invoke the same tool again — the server writes the
    token to ~/.aws/sso/cache and silently retries.
```
```
List ECS clusters on mycompany-na4
  → list_ecs_clusters(profile_name="mycompany-na4")
```
```
Give me every EC2 in the mycompany-tst3 env tagged as prod
  → describe_env_instances(profile_name="mycompany-tst3", env="prod")
```
```
Search CloudWatch on mycompany-eu1 for "OutOfMemoryError" in /aws/lambda/foo
in the last hour
  → query_log_groups(profile_name="mycompany-eu1",
     log_group_names=["/aws/lambda/foo"], pattern="OutOfMemoryError",
     since_hours=1)
```

**Reminder:** for MyCompany, FortiClient VPN must be ON before the sign-in
page loads. The server surfaces this warning on every SSO response.

### IAM / MyCompany (existing prompts — unchanged)

**Session / auth**

```
List my AWS profiles
```
```
Check auth status on stg4
```
```
Refresh my awsume session for uk2
```

**Inspection**

```
List ECS clusters on stg4
```
```
Get ECS service status for scheduling in eu1 on 27.0
```
```
Show me the ASG pool status for agentproxy in uk2 version 4.0
```
```
List every component running in tst3
```
```
What's running in eu1 on version 27.0? Just ECS.
```

**Fleet operations (refresh)**

```
Find ECS services matching "agent" in uk2 on version 27.0 that I could refresh
```
```
Find the ASG pools for recording in stg4 on 27.0
```
```
Refresh the scheduling ECS service in tst3 on 27.0
```
```
Refresh all agentproxy ASG pools in eu1 on 4.0 in parallel
```

**CloudWatch Logs (MyCompany naming)**

Log groups follow the convention `/<env>/<component>_<version>` — e.g.
`/au1/ACD_27.0`, `/au1/agent-compliance_27.0`, `/au1/inputfilter_8.0`.

MyCompany log lines are JSON, so CloudWatch auto-parses their fields
(`logType`, `logLevel`, `statusCode`, `traceId`, `requestURL`, `logger`,
`threadName`, `elapsedTime`, …). You can filter by / group by / project
any of them.

```
List CloudWatch log groups in au1
```
```
Which log groups exist for ACD in au1?
```
```
Pull the latest errors from ACD in au1 on 27.0 from the last 30 minutes
```
```
Count errors in the last hour for ACD, agent-compliance, and inputfilter in au1
```
```
Search the au1 ACD 27.0 logs for "ORA-00028" in the last 2 hours
```
```
Latest healthcheck results for ACD in au1 on 27.0
```
```
Find errors across ACD (all versions) in au1 in the last 15 minutes
```

**Ranking / grouped counts** (uses `group_by`)

```
Which logstream has the most errors on ACD in au1 (27.0) in the last hour?
```
```
Count HTTP status codes on ACD access logs in au1 (27.0) in the last 30 minutes
```
```
Group ACD 27.0 access logs by statusCode in the last hour
```
```
Which task is producing the most FATAL logs on scheduling in eu1 (27.0) today?
```

**JSON field filters** (uses `filters` / `raw_filter` / `fields`)

```
Show me only application ERROR logs on ACD in au1 (27.0)
```
```
Pull the last 20 access log entries for ACD 27.0 with statusCode 500
```
```
5xx errors on ACD 27.0 access logs by logstream in the last 2 hours
  → filters={"logType":"access"}, raw_filter="statusCode >= 500",
    count_only=True, group_by="@logStream"
```
```
Slow requests on ACD 27.0 access logs — elapsedTime > 1000 ms in last hour
  → raw_filter="elapsedTime > 1000", filters={"logType":"access"},
    fields=["statusCode","requestURL","elapsedTime"]
```

### AWS CLI escape hatch (`run_aws_cli`)

When the specialized tools above don't cover the ask, drop to a raw
`aws …` command. Works for both IAM and SSO profiles — the tool injects
the right session profile (`--profile <profile>-session` for IAM,
`--profile <profile>` for SSO) and the profile's region unless you
specify otherwise. On SSO expiry it surfaces the same `needs_sso_login`
URL flow.

*Read-only — runs immediately:*

```
List all my S3 buckets on stg4
  → run_aws_cli(command="aws s3api list-buckets", profile_name="stg4")
```
```
Get caller identity on every MyCompany account in parallel
  → run_aws_cli(command="aws sts get-caller-identity",
     profile_names=["mycompany-na3","mycompany-na4","mycompany-eu1","mycompany-au1","mycompany-tst3"])
```
```
Which IAM roles have "admin" in their name on stg4?
  → run_aws_cli(command="aws iam list-roles --query 'Roles[?contains(RoleName,`admin`)].RoleName'",
     profile_name="stg4")
```
```
Describe the ECS task definition family "scheduling-u27-service-eu1" across stg4, eu1, uk2 in parallel
  → run_aws_cli(command="aws ecs list-task-definitions --family-prefix scheduling-u27-service",
     profile_names=["stg4","eu1","uk2"])
```

*Mutating — two-step confirmation:*

```
Stop the EC2 instance i-0abcdef on stg4
  → run_aws_cli(command="aws ec2 stop-instances --instance-ids i-0abcdef", profile_name="stg4")
  ← needs_confirmation=True — shows the exact resolved command
  → after user OK: run_aws_cli(..., confirmed_by_user=True)
```
```
Roll the ASG "recording-u27-asg-stg4"
  → run_aws_cli(command="aws autoscaling start-instance-refresh --auto-scaling-group-name recording-u27-asg-stg4",
     profile_name="stg4")
  ← needs_confirmation=True
```
```
Preview what would run on stg4 + eu1 without executing
  → run_aws_cli(command="aws ec2 describe-instances --filters Name=tag:Env,Values=prod",
     profile_names=["stg4","eu1"], dry_run=true)
```

> Every command starts with `aws`. Pipes, redirects, and shell chaining
> (`&&`, `|`, `>`, backticks, `$()`) are rejected before the command
> reaches `subprocess.run(shell=False)` — the bridge cannot spawn a
> subshell no matter what the AI sends. Mutating commands (anything whose
> verb isn't on the read-only allowlist) require explicit
> `confirmed_by_user=True`; the tool always shows you the exact resolved
> argv first.

## Troubleshooting SSO

| Symptom | Fix |
|---------|-----|
| Every SSO tool call comes back with `sso_start_error` mentioning `Could not connect` / `Name or service not known`. | FortiClient VPN is OFF. MyCompany SSO endpoints (`identitycenter.amazonaws.com`, `device.sso.<region>.amazonaws.com`) are only reachable on-VPN. Turn VPN on and retry. |
| Response has `ambiguous_profile: true` with both an IAM and an SSO profile in `matches`. | Substring hits both. Spell more of the SSO profile's name (e.g. `mycompany-na3` instead of `na3`) or explicitly pick from `matches_detailed`. |
| `sso_status` returns `cached_token_valid: false` even right after `aws sso login` on the host. | Check that the compose mount `${HOST_AWS_DIR:-${HOME}/.aws}:/root/.aws:rw` is present and that `~/.aws/sso/cache/` has fresh files (`ls -lrth ~/.aws/sso/cache/`). |
| `sso_login(wait=true)` returns instantly with `needs_sso_login: true` and `wait_skipped: true`. | This is intentional. The server refuses to block on `wait=true` on a fresh device-authorization because the URL is only in this response — blocking would hide it from the user. Show the URL, wait for approval, then either retry the original AWS tool (recommended) or call `sso_login(wait=true)` again (this second call will honor the wait). |
| `sso_login(wait=true)` returns without `logged_in: true`. | Only meaningful on the SECOND+ call for the same login. The user didn't approve within `max_wait_seconds`. Raise `max_wait_seconds`, or just call any AWS tool after they approve — the pending state will be picked up automatically. |
