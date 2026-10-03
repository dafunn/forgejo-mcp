# forgejo-mcp

MCP server for Forgejo Actions (CI/CD): list runs, resolve jobs, read build logs.

Configured entirely from the environment.

**Requires Forgejo >= 16.0**, the first version with the Actions log REST API.

## Environment contract

| Variable | Default | Meaning |
|---|---|---|
| `FORGEJO_URL` | _(required)_ | Base URL, e.g. `https://forge.example.com` |
| `FORGEJO_TOKEN_CMD` | - | Preferred. Shell command whose stdout is the token |
| `FORGEJO_TOKEN` | - | Token (or Basic password) inline |
| `FORGEJO_TOKEN_FILE` | - | Path to a token file. No default: opt in explicitly |
| `FORGEJO_TOKEN_CMD_TIMEOUT` | `30` | Seconds to wait for the token command |
| `FORGEJO_USER` | - | If set, authenticate with HTTP Basic instead of the `token` scheme |
| `FORGEJO_DEFAULT_REPO` | - | `owner/name` used when a tool's `repo` argument is omitted |
| `FORGEJO_VERIFY_TLS` | `true` | Set `false` to skip certificate verification |
| `FORGEJO_TIMEOUT` | `30` | Per-request timeout in seconds |
| `MCP_TRANSPORT` | `stdio` | `sse` to serve over HTTP instead of a spawned pipe |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `3000` | SSE bind address |
| `MCP_ALLOWED_HOSTS` | _(required for sse)_ | Host header values clients use, e.g. `forgejo-mcp:3000`; `*` disables the check |

Resolution order is `FORGEJO_TOKEN_CMD` -> `FORGEJO_TOKEN` -> `FORGEJO_TOKEN_FILE`.
A token command that exits non-zero is fatal; it never falls back to another
source.

### Which auth scheme?

A personal API token uses `Authorization: token <tok>`. A user password, or a
token reused from a git credential store, works only over HTTP Basic; set
`FORGEJO_USER` for those.

## Holding the credential

`FORGEJO_TOKEN_CMD` reads the token from a secret store when the server starts,
so the token is never in a file or a client config. Example commands:

| Store | Command |
|---|---|
| gpg + `gpg-agent` | `gpg -q -d ~/.secrets/forgejo-token.gpg` |
| Bitwarden / Vaultwarden | `bw get password forgejo-api` |
| git credential store | `printf 'protocol=https\nhost=forge.example.com\n\n' \| git credential fill \| sed -n 's/^password=//p'` |
| `ansible-vault` | `ansible-vault view --vault-password-file <pw> token.yml` |
| `systemd-creds` | `systemd-creds decrypt /etc/creds/forgejo.cred -` |

The server only issues GETs against `/actions/*`, so a read-only token is enough.

## Tools

| Tool | Purpose |
|---|---|
| `forgejo_build_status(repo, limit)` | Recent runs: number, id, status, workflow, title |
| `forgejo_run_info(repo, run_number)` | One run by its run number, plus its jobs |
| `forgejo_run_jobs(repo, run_number)` | Jobs for a run, with the `job_id` needed for logs |
| `forgejo_job_log(repo, job_id, tail)` | Log for one job |
| `forgejo_run_log(repo, run_number, tail)` | Logs for every job in a run |
| `forgejo_failed_logs(repo, tail, scan)` | Newest failing run, failed jobs' logs only |

## Run it

```bash
uv sync --frozen
FORGEJO_URL=https://forge.example.com \
  FORGEJO_TOKEN_CMD="gpg -q -d ~/.secrets/forgejo-token.gpg" \
  .venv/bin/python server.py
```

Over SSE, for a client that cannot spawn a process:

```bash
FORGEJO_URL=https://forge.example.com \
  FORGEJO_TOKEN_FILE=/run/secrets/forgejo-token \
  MCP_TRANSPORT=sse MCP_HOST=0.0.0.0 MCP_ALLOWED_HOSTS=forgejo-mcp:3000 \
  .venv/bin/python server.py
```

`MCP_ALLOWED_HOSTS` has no default; over SSE the server refuses to start
without it.

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/). `uv sync --frozen`
installs the locked dependencies into `.venv`.

Tests: `uv sync --frozen --extra dev && .venv/bin/python -m pytest tests/`

Container image: `docker build .` (or `podman build .`).

Register it with an MCP client by pointing at `.venv/bin/python server.py` with
the environment above.
