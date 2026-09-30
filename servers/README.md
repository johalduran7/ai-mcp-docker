# MCP Servers

Each subfolder is one MCP server. See the individual READMEs for tool
tables, configuration, and example prompts.

| Folder | Server | README |
|--------|--------|--------|
| [jira](jira/) | `lvx-jira` | [jira/README.md](jira/README.md) |
| [confluence](confluence/) | `lvx-confluence` | [confluence/README.md](confluence/README.md) |
| [aws](aws/) | `lvx-aws` | [aws/README.md](aws/README.md) |
| [gitlab](gitlab/) | `lvx-gitlab` | [gitlab/README.md](gitlab/README.md) |
| [github](github/) | `lvx-github` | [github/README.md](github/README.md) |
| [grafana](grafana/) | `lvx-grafana` | [grafana/README.md](grafana/README.md) |
| [opensearch](opensearch/) | `lvx-opensearch` | [opensearch/README.md](opensearch/README.md) |
| [argocd](argocd/) | `lvx-argocd` | [argocd/README.md](argocd/README.md) |

Convention for new servers:

- One folder per server.
- Keep `server.py`, `requirements.txt`, `run_server.sh`, and `README.md`
  together.
- Add `runtime-<name>` and `versioned-<name>` stages to the shared
  `Dockerfile`, and a `lvx-<name>` service to `docker-compose.yml`.
- See the root [README.md](../README.md) → *Adding a new MCP server* for the
  full checklist.
