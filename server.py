"""forgejo-mcp: Forgejo Actions (CI/CD) MCP server.

Configured from the environment:

    FORGEJO_URL           base URL, e.g. https://forge.example.com   (required)
    FORGEJO_TOKEN_CMD     shell command whose stdout is the token (preferred)
    FORGEJO_TOKEN         API token (or Basic password), inline
    FORGEJO_TOKEN_FILE    path to a token file
    FORGEJO_USER          if set, authenticate with HTTP Basic instead of the
                          `token` scheme
    FORGEJO_DEFAULT_REPO  owner/name used when a tool's `repo` arg is omitted
    REPO_ALLOWLIST        comma-separated owner/name entries; when set, only these
                          repos can be read, matched case-insensitively
    FORGEJO_VERIFY_TLS    "false" to skip verification (default true)
    FORGEJO_TIMEOUT       per-request seconds (default 30)

Run numbers and run IDs come from different endpoints: `/actions/tasks` reports
`run_number` but its `id` is a task id; `/actions/runs` reports the run `id` and
`index_in_repo`, which is the run number. Logs belong to jobs:
runs -> runs/{run_id}/jobs -> jobs/{job_id}/logs. They are read through the API
because the web UI log route accepts only a session cookie.

Requires Forgejo >= 16.0, the first version with the Actions log REST API.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

FORGEJO_URL = os.getenv("FORGEJO_URL", "").rstrip("/")
FORGEJO_USER = os.getenv("FORGEJO_USER", "").strip()
DEFAULT_REPO = os.getenv("FORGEJO_DEFAULT_REPO", "")
VERIFY_TLS = os.getenv("FORGEJO_VERIFY_TLS", "true").lower() not in ("false", "0", "no")
TIMEOUT = float(os.getenv("FORGEJO_TIMEOUT", "30"))
TOKEN_CMD_TIMEOUT = float(os.getenv("FORGEJO_TOKEN_CMD_TIMEOUT", "30"))
# How many recent runs are searched when resolving a run number or a job id.
RUN_WINDOW = 100


def _parse_allowlist(raw: str) -> frozenset[str]:
    """Case-folded set of the owner/name entries in `raw`; empty means no restriction.

    Fails at import so a typo cannot silently leave the server unrestricted: a value
    with anything but whitespace in it must yield at least one entry.
    """
    out = set()
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        parts = entry.split("/")
        if len(parts) != 2 or not all(parts) or ".." in entry:
            raise RuntimeError(f"REPO_ALLOWLIST entry {entry!r} is not 'owner/name'")
        out.add(entry.casefold())
    if raw.strip() and not out:
        raise RuntimeError(f"REPO_ALLOWLIST {raw.strip()!r} contains no owner/name entries")
    return frozenset(out)


REPO_ALLOWLIST = _parse_allowlist(os.getenv("REPO_ALLOWLIST", ""))
_default = DEFAULT_REPO.strip().strip("/")
if REPO_ALLOWLIST and _default and _default.casefold() not in REPO_ALLOWLIST:
    raise RuntimeError(f"FORGEJO_DEFAULT_REPO {_default!r} is not in REPO_ALLOWLIST")


def release_version() -> str:
    """The release this build reports to clients: FORGEJO_MCP_VERSION,
    stamped into the image from the forgejo-mcp-v* tag at build time, else `dev`."""
    return os.environ.get("FORGEJO_MCP_VERSION", "").strip() or "dev"


app = MCPServer("forgejo-mcp", version=release_version())


def _token() -> str:
    """Resolve the credential: FORGEJO_TOKEN_CMD, then FORGEJO_TOKEN, then FORGEJO_TOKEN_FILE.

    FORGEJO_TOKEN_CMD's stdout is the token, so the secret stays in whatever store
    the command reads from. Its stderr passes through so the command can prompt. A
    non-zero exit is fatal; it never falls back to a weaker source.
    """
    cmd = os.getenv("FORGEJO_TOKEN_CMD", "").strip()
    if cmd:
        try:
            out = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=TOKEN_CMD_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"FORGEJO_TOKEN_CMD timed out after {TOKEN_CMD_TIMEOUT}s "
                "(is an agent waiting on a passphrase prompt it cannot show?)"
            ) from None
        if out.returncode != 0:
            # Deliberately excludes stdout - that may be a partial secret.
            raise RuntimeError(
                f"FORGEJO_TOKEN_CMD failed (exit {out.returncode}): "
                f"{out.stderr.strip()[:200] or '(no stderr)'}"
            )
        tok = out.stdout.strip()
        if not tok:
            raise RuntimeError("FORGEJO_TOKEN_CMD produced no output")
        return tok
    tok = os.getenv("FORGEJO_TOKEN", "").strip()
    if tok:
        return tok
    path_env = os.getenv("FORGEJO_TOKEN_FILE", "")
    if path_env:
        path = Path(path_env)
        if path.is_file():
            return path.read_text().strip()
    return ""


def _client() -> httpx.Client:
    """Authenticated client: HTTP Basic when FORGEJO_USER is set, else the `token` scheme.

    A password, or a token reused from a git credential store, authenticates only
    over HTTP Basic.
    """
    if not FORGEJO_URL:
        raise RuntimeError("FORGEJO_URL is not set")
    headers = {"Accept": "application/json"}
    tok = _token()
    auth = None
    if FORGEJO_USER and tok:
        auth = httpx.BasicAuth(FORGEJO_USER, tok)
    elif tok:
        headers["Authorization"] = f"token {tok}"
    return httpx.Client(
        base_url=FORGEJO_URL, headers=headers, auth=auth, timeout=TIMEOUT,
        verify=VERIFY_TLS, follow_redirects=False,
    )


# A refusal or a lookup that finds nothing raises ToolError: the SDK returns its
# message to the model as an error result and logs it without a traceback. Any
# other exception is reported as a crash, with the reason withheld.


def _repo(repo: str) -> str:
    r = (repo or DEFAULT_REPO).strip().strip("/")
    if not r or r.count("/") != 1:
        raise ToolError(
            f"repo must be 'owner/name' (got {r!r}); set FORGEJO_DEFAULT_REPO or pass repo="
        )
    if REPO_ALLOWLIST and (".." in r or r.casefold() not in REPO_ALLOWLIST):
        raise ToolError(f"repo {r!r} is not in REPO_ALLOWLIST")
    return r


def _get(path: str, params: dict | None = None) -> httpx.Response:
    with _client() as c:
        resp = c.get(path, params=params)
    if resp.status_code == 303:
        # The web UI routes bounce tokens to /user/login. Hitting this means a
        # non-/api/v1 path was requested.
        raise RuntimeError(f"{path} redirected to login - not an API route")
    resp.raise_for_status()
    return resp


def _runs(repo: str, limit: int) -> list[dict[str, Any]]:
    data = _get(f"/api/v1/repos/{repo}/actions/runs", {"limit": limit}).json()
    return data.get("workflow_runs") or []


def _find_run(repo: str, run_number: int) -> dict[str, Any] | None:
    """Resolve a run number to its run record via `index_in_repo`.

    `/actions/tasks` is not used: its `id` is a task id, not a run id.
    """
    for r in _runs(repo, RUN_WINDOW):
        if r.get("index_in_repo") == run_number:
            return r
    return None


def _jobs(repo: str, run_id: Any) -> list[dict[str, Any]]:
    jobs = _get(f"/api/v1/repos/{repo}/actions/runs/{run_id}/jobs").json()
    if isinstance(jobs, dict):
        jobs = jobs.get("workflow_jobs") or jobs.get("jobs") or []
    return jobs


def _job_in_recent_runs(repo: str, job_id: int) -> bool:
    """Whether `job_id` belongs to one of the repo's recent runs.

    Used when an allowlist is set, so a job id is never read through an allowed
    repo's path on the assumption that the forge rejects ids from other repos.
    """
    return any(
        j.get("id") == job_id
        for run in _runs(repo, RUN_WINDOW)
        for j in _jobs(repo, run["id"])
    )


def _fmt_run(r: dict[str, Any]) -> str:
    return (
        f"#{r.get('index_in_repo')} (run id {r.get('id')})  "
        f"{r.get('status', '?')}  {r.get('workflow_id', '')}  "
        f"{str(r.get('title', ''))[:60]}"
    )


@app.tool()
def forgejo_build_status(repo: str = "", limit: int = 15) -> str:
    """Recent Actions runs: run number, run id, status, workflow, title."""
    rp = _repo(repo)
    runs = _runs(rp, limit)
    if not runs:
        return f"No workflow runs for {rp}."
    return f"{rp} - {len(runs)} run(s)\n" + "\n".join("  " + _fmt_run(r) for r in runs)


@app.tool()
def forgejo_run_info(repo: str = "", run_number: int = 0) -> str:
    """Details for one run, addressed by its RUN NUMBER (the #N in the UI)."""
    rp = _repo(repo)
    r = _find_run(rp, run_number)
    if not r:
        raise ToolError(f"No run #{run_number} in the last {RUN_WINDOW} runs of {rp}.")
    jobs = _jobs(rp, r["id"])
    lines = [
        f"Run #{r.get('index_in_repo')} (id {r['id']}) - {r.get('status')}",
        f"  workflow: {r.get('workflow_id')}",
        f"  title:    {r.get('title')}",
        f"  commit:   {str(r.get('commit_sha', ''))[:12]}   event: {r.get('event')}",
        f"  jobs ({len(jobs)}):",
    ]
    for j in jobs:
        lines.append(f"    job_id={j.get('id')}  {j.get('name')}  {j.get('status')}")
    return "\n".join(lines)


@app.tool()
def forgejo_run_jobs(repo: str = "", run_number: int = 0) -> str:
    """Jobs for a run, with the job_id needed to fetch logs."""
    rp = _repo(repo)
    r = _find_run(rp, run_number)
    if not r:
        raise ToolError(f"No run #{run_number} in the last {RUN_WINDOW} runs of {rp}.")
    jobs = _jobs(rp, r["id"])
    return "\n".join(
        f"job_id={j.get('id')}  {j.get('name')}  {j.get('status')}" for j in jobs
    ) or f"No jobs for run #{run_number}."


@app.tool()
def forgejo_job_log(repo: str = "", job_id: int = 0, tail: int = 80) -> str:
    """Log for one job by JOB id (from forgejo_run_jobs), last `tail` lines."""
    if not job_id:
        raise ToolError("job_id is required - get it from forgejo_run_jobs.")
    rp = _repo(repo)
    if REPO_ALLOWLIST and not _job_in_recent_runs(rp, int(job_id)):
        raise ToolError(f"job {job_id} was not found in the recent runs of {rp}")
    text = _get(f"/api/v1/repos/{rp}/actions/jobs/{job_id}/logs").text
    lines = text.strip().splitlines()
    return "\n".join(lines[-tail:]) if lines else "(empty log)"


@app.tool()
def forgejo_run_log(repo: str = "", run_number: int = 0, tail: int = 80) -> str:
    """Logs for every job in a run, addressed by RUN NUMBER."""
    rp = _repo(repo)
    r = _find_run(rp, run_number)
    if not r:
        raise ToolError(f"No run #{run_number} in the last {RUN_WINDOW} runs of {rp}.")
    jobs = _jobs(rp, r["id"])
    out = []
    for j in jobs:
        text = _get(f"/api/v1/repos/{rp}/actions/jobs/{j['id']}/logs").text
        body = "\n".join(text.strip().splitlines()[-tail:])
        out.append(f"===== job {j.get('name')} (id {j.get('id')}) [{j.get('status')}]\n{body}")
    return "\n\n".join(out) or f"No jobs for run #{run_number}."


@app.tool()
def forgejo_failed_logs(repo: str = "", tail: int = 60, scan: int = 20) -> str:
    """Logs for failed jobs in the most recent failing run.

    Scans back `scan` runs for the newest non-success run, then returns only its
    failed jobs' logs.
    """
    rp = _repo(repo)
    runs = _runs(rp, scan)
    bad = next((r for r in runs if r.get("status") not in ("success", "running", "waiting")), None)
    if not bad:
        return f"No failing run in the last {len(runs)} run(s) of {rp}."
    jobs = _jobs(rp, bad["id"])
    failed = [j for j in jobs if j.get("status") not in ("success",)]
    out = [f"Newest failing run: {_fmt_run(bad)}"]
    for j in failed:
        text = _get(f"/api/v1/repos/{rp}/actions/jobs/{j['id']}/logs").text
        out.append(
            f"===== job {j.get('name')} (id {j.get('id')}) [{j.get('status')}]\n"
            + "\n".join(text.strip().splitlines()[-tail:])
        )
    return "\n\n".join(out)


# -- Transport ----------------------------------------------------------------
# stdio by default; `sse` serves the same tools over HTTP, for clients that
# cannot spawn a process.
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1").strip()
MCP_PORT = int(os.getenv("MCP_PORT", "3000"))
MCP_ALLOWED_HOSTS = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]


def _transport_security():
    """Host-header policy for the SSE transport.

    mcp 2.x enables DNS-rebinding protection with an empty allowed-host list, which
    rejects every request with a 421. The operator names the hostnames this server
    answers to; `*` turns the check off.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if not MCP_ALLOWED_HOSTS:
        raise RuntimeError(
            "MCP_ALLOWED_HOSTS is required when MCP_TRANSPORT=sse - set the host:port "
            "values clients use to address this server (e.g. 'forgejo-mcp:3000'), or '*' "
            "to disable the Host-header check"
        )
    if MCP_ALLOWED_HOSTS == ["*"]:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(allowed_hosts=MCP_ALLOWED_HOSTS, allowed_origins=MCP_ALLOWED_HOSTS)


def build_sse_app():
    """Build the ASGI app for the SSE transport. A separate function so tests can construct it."""
    return app.sse_app(host=MCP_HOST, transport_security=_transport_security())


if __name__ == "__main__":
    if MCP_TRANSPORT == "sse":
        app.run(transport="sse", host=MCP_HOST, port=MCP_PORT,
                transport_security=_transport_security())
    else:
        app.run()
