---
name: local-agent-delegate
description: Use when delegating bounded local coding-agent tasks to a local backend from a supervising AI agent or MCP client, especially token-heavy repo exploration, file comparisons, audits, compact first-pass findings, or candidate patches. Triggers on Local Agent Delegate, token saving, remote model compute with local file access, local Pi backend, or asking a local agent to inspect or patch a local repo.
---

# Local Agent Delegate

Use Local Agent Delegate when a supervising AI agent needs a local coding-agent
helper that can inspect the current local workspace while using remote model
compute. The current backend is the Pi CLI.

## Decision Boundary

- Honor `LOCAL_AGENT_DELEGATE_LEAN`, `LOCAL_AGENT_DELEGATE_GOAL`, and `LOCAL_AGENT_DELEGATE_THINKING`; if unsure, call
  `local_agent_delegate_policy` and follow its `redelegation_threshold`.
- Do not inspect `LOCAL_AGENT_DELEGATE_*` with shell commands such as `printenv`; these
  settings are scoped to the MCP server process and may not be present in a
  project shell.
- Use `local_agent_delegate_run_start` for read-only local repo investigation, then poll
  `local_agent_delegate_job_status`, wait with `local_agent_delegate_job_wait`, and fetch
  `local_agent_delegate_job_result`.
- When the goal is `save-on-tokens`, use a mandatory wait-first workflow. After
  `local_agent_delegate_run_start`, immediately call
  `local_agent_delegate_job_wait include_details=false` before primary-agent
  code-index, `rg`, `find`, `ls`, `sed`, `cat`, or other local file exploration.
  Do not start direct "focused checks" in parallel unless the user explicitly
  requested parallel work or the job has returned, failed, timed out, or is
  clearly off-track.
- Re-delegate for each new bounded exploration phase when the expected local
  inspection meets `redelegation_threshold`. Verify the previous result's named
  evidence first, then delegate the next broad subtask instead of expanding
  primary-agent reads.
- Use `local_agent_delegate_patch_start` for self-contained edits that can be reviewed
  as a diff, then poll/fetch the same job lifecycle tools.
- Completed `local_agent_delegate_job_wait` or `local_agent_delegate_job_result` calls with
  `include_details=false` return the final text plus minimal counters and
  artifact paths. They omit activity tails, session metadata, raw stdout/stderr
  JSON tails, and large diffs.
- A completed result may have `result_state: "partial_timeout"` when the backend
  timed out after producing useful assistant text or a patch diff. Use the
  partial result as advisory evidence before deciding whether to narrow and
  re-delegate.
- For long jobs, prefer `local_agent_delegate_job_wait` with a bounded timeout. If
  polling, inspect compact `activity_tail`, `running_actions`, `counters`, and
  artifact paths; cancel the job if it is clearly off-track.
- For token saving, call `local_agent_delegate_job_wait` with `include_details=false`
  first. Use `include_details=true` only when diagnosing a failed or off-track
  job.
- Do not call `local_agent_delegate_run` or `local_agent_delegate_patch`. They were removed from
  the tool surface because synchronous backend calls can exceed common tool-call
  timeouts.
- Do not use a remote backend for local file work. The backend must run on the
  machine with the files it needs to inspect.

## Verification Budget

- After the backend returns, verify only the named files, symbols, commands, or claims
  needed for correctness.
- If broader primary-agent exploration is still needed after the job returns,
  fails, times out, or is clearly off-track, first state why the delegated
  result was insufficient. If the follow-up still crosses
  `redelegation_threshold`, start a narrower delegated job; otherwise continue
  with the smallest useful local search.
- Treat `counters.estimated_primary_tokens_avoided_approx` as a rough trend
  signal for whether delegation avoided primary-agent context, not billing data.

## Re-delegation Threshold

- `off`: do not re-delegate unless the user explicitly asks.
- `conservative`: re-delegate only for clearly bounded follow-up work that would
  otherwise require about 6+ file reads/searches, cross-module tracing, or an
  independent second review.
- `balanced`: re-delegate when a new bounded subtask would otherwise require
  about 3+ file reads/searches or an unfamiliar subsystem.
- `aggressive`: re-delegate when a new bounded subtask would otherwise require
  about 2+ file reads/searches or any new subsystem after the first scout.
- `LOCAL_AGENT_DELEGATE_GOAL=save-on-tokens` lowers the active threshold by one
  level, except `off` stays off.

## Delegation Level

- `off`: do not delegate unless the user explicitly asks.
- `conservative`: use the backend for high-token read-only repo investigation when the
  question is clearly bounded.
- `balanced`: use the backend for token-heavy read-only exploration and comparisons;
  patch mode is for reviewable self-contained diffs.
- `aggressive`: prefer the backend for first-pass repo exploration, comparisons, audits,
  and candidate patches while still reviewing returned diffs.

## Delegation Goal

- `balanced`: use the backend when it is likely to save time or provide useful confidence;
  keep tasks and returned results scoped.
- `save-on-tokens`: use the backend for token-heavy local exploration; ask for compact
  findings and avoid duplicating broad file reads in the supervising agent
  unless needed for verification.
- `parallel-review`: use the backend as an independent second opinion while the
  supervising agent may continue local work; this favors confidence and
  wall-clock overlap over token savings.
- `unrestricted`: let the backend explore broadly within the explicit task and safety
  rules; still monitor, cancel off-track jobs, and verify important claims.

## Thinking

- `default`: Local Agent Delegate does not pass a `--thinking` flag.
- `off`, `minimal`, `low`, `medium`, `high`, `xhigh`: Local Agent Delegate passes the
  configured value as `pi --thinking <value>`. Per-job `thinking` can override
  the MCP server default for hard tasks.
- For token saving, prefer `default` or `off` first. Escalate a single job to
  higher thinking only after the scope is narrow enough to avoid large repeated
  tool context.

## Safety Rules

- Always pass an explicit local `cwd`.
- Treat backend output as advisory; the supervising agent must verify important
  claims.
- For edits, prefer `local_agent_delegate_patch_start`; it uses a temporary git worktree
  and returns a diff without applying it to the current tree.
- If a delegated job is no longer useful, call `local_agent_delegate_job_cancel` instead of
  abandoning a long-running request.
- Do not ask the backend to handle secrets, credentials, production incident conclusions,
  or final correctness decisions.

## Good Uses

- Ask the backend to map a local subsystem and report likely files/functions to inspect.
- Ask the backend to compare several files or implementations and return compact findings.
- Ask the backend to inspect the next bounded subtask discovered during
  verification when it crosses `redelegation_threshold`.
- Ask the backend to produce a candidate patch when the requested change is self-contained
  and the returned diff can be reviewed.
- Ask the backend for a second-pass critique after the supervising agent has narrowed the
  scope.

## Avoid

- Broad, underspecified tasks.
- Running supervising-agent code-index or shell exploration in parallel with the
  backend when the objective is saving primary-agent context.
- Dirty-tree patch tasks where uncommitted local changes matter.
- Remote-host file assumptions. Backend file access is local to the machine
  running the MCP server.
