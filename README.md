# local-agent-delegate

> `local-agent-delegate` is an unofficial third-party MCP bridge for the Pi
> coding agent CLI. It is not affiliated with, endorsed by, or maintained by
> Earendil Works or the Pi project.

`local-agent-delegate` is a stdio MCP server for delegating bounded local
coding-agent tasks to a local agent backend. The current backend is the `pi`
CLI: Pi runs on this machine, reads local workspace files, and uses the model
endpoints configured in `~/.pi/agent/models.json`.

The server is intentionally bounded and token-aware:

- `local_agent_delegate_policy` reports delegation level, objective, and
  thinking mode from the MCP server environment.
- `local_agent_delegate_status` checks backend availability and returns install
  hints instead of failing when Pi is missing.
- `local_agent_delegate_run_start` starts read-only tasks with only `read`,
  `grep`, `find`, and `ls`.
- `local_agent_delegate_patch_start` starts patch tasks in a temporary clean git
  worktree and returns a diff artifact for review.
- `local_agent_delegate_job_status`, `local_agent_delegate_job_wait`,
  `local_agent_delegate_job_result`, and `local_agent_delegate_job_cancel` let an
  MCP client watch, wait for, fetch, or stop long-running jobs.
- Completed results are compact by default. Raw streams, assistant text, stderr,
  activity, sessions, and patch diffs are written under
  `~/.cache/local-agent-delegate/jobs/<job_id>/` by default.
- Counters include raw stream byte counts, delivered assistant chars, and an
  approximate avoided-token proxy so callers can audit whether delegation is
  saving primary-agent context.

There is no remote-host backend tool. If a task depends on local files, run the
local backend on the machine that has those files, then point that backend at
your preferred remote model endpoint.

## Requirements

- Python 3.10 or newer.
- An MCP-capable coding agent that supports local stdio MCP servers.
- The Pi coding agent CLI installed and configured. Confirm `pi --version`
  works, then configure `~/.pi/agent/models.json`.
- Either `pi` on `PATH`, or `LOCAL_AGENT_DELEGATE_PI_BIN` set to the Pi
  executable.

`local-agent-delegate` does not install Pi automatically. If Pi is unavailable,
`local_agent_delegate_status` returns `available=false`, a reason such as
`backend_not_found`, and an `install_hint`. Job-start tools fail fast when Pi is
missing because they cannot do useful local-agent work without it.

## Install

From a clone:

```bash
python3 -m pip install -e .
```

If the package is published to your package index:

```bash
python3 -m pip install local-agent-delegate
```

After installation, use the console script:

```bash
local-agent-delegate-mcp
```

For development from a checkout, use the script path instead:

```bash
python3 /path/to/local-agent-delegate/mcp/local-agent-delegate-mcp.py
```

## Configure Your Coding Agent

Use `local-agent-delegate-mcp` as a local stdio MCP server. Put the equivalent
configuration wherever your agent stores MCP servers.

Common environment variables:

- `LOCAL_AGENT_DELEGATE_LEAN`: `off`, `conservative`, `balanced`, or
  `aggressive`. Defaults to `balanced`.
- `LOCAL_AGENT_DELEGATE_GOAL`: `balanced`, `save-on-tokens`, `parallel-review`,
  or `unrestricted`. Defaults to `balanced`.
- `LOCAL_AGENT_DELEGATE_THINKING`: `default`, `off`, `minimal`, `low`, `medium`,
  `high`, or `xhigh`. `default` omits Pi's `--thinking` flag.
- `LOCAL_AGENT_DELEGATE_MODEL`: optional model name from `pi --list-models`.
  Omit it to use Pi's configured default.
- `LOCAL_AGENT_DELEGATE_PI_BIN`: optional path to the `pi` executable.
- `LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT`: defaults to
  `~/.cache/local-agent-delegate/jobs`.
- `LOCAL_AGENT_DELEGATE_TARGET_RESULT_CHARS`: defaults to `12000`.
- `LOCAL_AGENT_DELEGATE_JOB_TTL_SECONDS`: defaults to one hour.

`LOCAL_AGENT_DELEGATE_LEAN` also controls when a primary agent should
re-delegate a follow-up exploration phase:

- `off`: explicit user request only.
- `conservative`: about 6+ file reads/searches, cross-module tracing, or an
  independent second review.
- `balanced`: about 3+ file reads/searches or an unfamiliar subsystem.
- `aggressive`: about 2+ file reads/searches or any new subsystem after the
  first scout.

`LOCAL_AGENT_DELEGATE_GOAL=save-on-tokens` lowers that threshold by one level,
except `off` remains off.

Generic MCP config:

```json
{
  "mcpServers": {
    "local-agent-delegate": {
      "command": "local-agent-delegate-mcp",
      "env": {
        "LOCAL_AGENT_DELEGATE_LEAN": "balanced",
        "LOCAL_AGENT_DELEGATE_GOAL": "save-on-tokens",
        "LOCAL_AGENT_DELEGATE_THINKING": "default",
        "LOCAL_AGENT_DELEGATE_MODEL": "provider/model-name",
        "LOCAL_AGENT_DELEGATE_PI_BIN": "/path/to/pi"
      }
    }
  }
}
```

Codex example in `~/.codex/config.toml`:

```toml
[mcp_servers.local_agent_delegate]
command = "local-agent-delegate-mcp"
enabled = true
startup_timeout_sec = 30

[mcp_servers.local_agent_delegate.env]
LOCAL_AGENT_DELEGATE_LEAN = "balanced"
LOCAL_AGENT_DELEGATE_GOAL = "save-on-tokens"
LOCAL_AGENT_DELEGATE_THINKING = "default"
LOCAL_AGENT_DELEGATE_MODEL = "provider/model-name"
LOCAL_AGENT_DELEGATE_PI_BIN = "/path/to/pi"
```

Claude Code local stdio example:

```bash
claude mcp add --transport stdio \
  --env LOCAL_AGENT_DELEGATE_LEAN=balanced \
  --env LOCAL_AGENT_DELEGATE_GOAL=save-on-tokens \
  --env LOCAL_AGENT_DELEGATE_THINKING=default \
  local-agent-delegate -- local-agent-delegate-mcp
```

Claude Code project-scoped `.mcp.json` example:

```json
{
  "mcpServers": {
    "local-agent-delegate": {
      "command": "local-agent-delegate-mcp",
      "env": {
        "LOCAL_AGENT_DELEGATE_GOAL": "save-on-tokens"
      }
    }
  }
}
```

GitHub Copilot in VS Code example for `.vscode/mcp.json`:

```json
{
  "servers": {
    "local-agent-delegate": {
      "type": "stdio",
      "command": "local-agent-delegate-mcp",
      "env": {
        "LOCAL_AGENT_DELEGATE_GOAL": "save-on-tokens"
      }
    }
  }
}
```

For source-checkout development, replace `command = "local-agent-delegate-mcp"`
with `command = "python3"` and add the absolute script path as an arg:

```json
{
  "command": "python3",
  "args": ["/path/to/local-agent-delegate/mcp/local-agent-delegate-mcp.py"]
}
```

## Optional Agent Guidance

Some agents will discover the tools from their descriptions alone. If you want
the primary agent to use delegation proactively, add a short instruction in that
agent's global or project guidance:

```md
Before broad or token-heavy repo exploration, call
`local_agent_delegate_policy`; if it allows delegation, start a bounded
read-only scout job with `local_agent_delegate_run_start`, wait with
`local_agent_delegate_job_wait include_details=false`, then verify only the
specific files, symbols, commands, or claims that matter. For each later
exploration phase, compare the expected local file reads/searches to
`redelegation_threshold`; if it meets the threshold, start a narrower follow-up
delegated job instead of expanding primary-agent exploration.
```

## Tools

- `local_agent_delegate_policy`: return the active delegation policy, including
  `redelegation_threshold`, `level_redelegation_guidance`,
  `goal_redelegation_guidance`, and the recommended delegation loop.
- `local_agent_delegate_status`: check backend and model availability.
- `local_agent_delegate_run_start`: start a read-only job and return a `job_id`.
- `local_agent_delegate_patch_start`: start a patch job in a disposable worktree
  and return a `job_id`.
- `local_agent_delegate_job_status`: inspect compact job state, activity,
  counters, session metadata, and artifact paths.
- `local_agent_delegate_job_wait`: wait for completion or a bounded timeout.
- `local_agent_delegate_job_result`: fetch compact final output. Result states
  include `complete`, `result_oversized`, and `partial_timeout`; the last means
  the backend timed out after producing useful assistant text or a patch diff.
- `local_agent_delegate_job_cancel`: cancel a running job.
- `local_agent_delegate_jobs`: list retained jobs in the MCP server process.

## Token-Saving Workflow

When the goal is to save primary-agent context, use the backend as a scout
rather than as a parallel transcript source. Reuse it for new bounded
exploration phases when the policy threshold says the follow-up is large enough:

1. Call `local_agent_delegate_policy`.
2. Start a bounded read-only scout job with `local_agent_delegate_run_start`.
3. Wait with `local_agent_delegate_job_wait` and `include_details=false`.
4. Read the compact result first.
5. Verify only the specific files, symbols, commands, or claims that matter.
6. If a new broad subtask remains, compare it to `redelegation_threshold`.
7. If the threshold is met, start a narrower follow-up delegated job; otherwise
   explicitly state why the scout was insufficient and continue with the
   smallest useful local search.
8. Treat `partial_timeout` as usable advisory evidence when it contains text or
   a diff, then decide whether to narrow and re-delegate.
9. Use `include_details=true` only to diagnose failed or off-track jobs.
10. Escalate `thinking` only after the scope is narrow.

Avoid running broad local `find`, `rg`, `sed`, or `cat` exploration in the
supervising agent while the delegated job is doing the same exploration. That
duplicates context instead of saving it.

The `counters.estimated_primary_tokens_avoided_approx` field is a heuristic
based on raw backend stream bytes minus delivered assistant text, using a coarse
4-bytes-per-token estimate. Treat it as a trend signal, not billing data.

## Smoke Test

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"local_agent_delegate_status","arguments":{}}}' \
  | local-agent-delegate-mcp
```

Source-checkout smoke test:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"local_agent_delegate_status","arguments":{}}}' \
  | python3 mcp/local-agent-delegate-mcp.py
```

Async read-only smoke test:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"local_agent_delegate_run_start","arguments":{"cwd":"'"$PWD"'","prompt":"Say LOCAL_AGENT_DELEGATE_OK and nothing else.","timeout":120}}}' \
  | python3 mcp/local-agent-delegate-mcp.py
```

Use the returned `job_id` with `local_agent_delegate_job_status` and
`local_agent_delegate_job_wait` or `local_agent_delegate_job_result`. Default
completed results include final text plus minimal counters and artifact paths;
they never include raw stdout/stderr JSON tails or large diffs. Pass
`include_details=true` when you need activity, session metadata, full counters,
or the full local artifact index.

Optional real Pi CLI flag smoke test:

```bash
LOCAL_AGENT_DELEGATE_REAL_PI_SMOKE=1 PYTHONPATH=src python3 -m pytest tests/test_runner.py::RunnerTests::test_real_pi_cli_supports_required_flags -q
```

This checks the installed `pi` binary for the resource/session flags used by
delegated jobs without running a model request.

## Notes

Patch mode refuses dirty repositories because delegated patch jobs run from a
temporary worktree at `HEAD`. The caller should review the returned diff before
applying anything.
