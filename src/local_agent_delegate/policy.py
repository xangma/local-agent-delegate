from __future__ import annotations

import os
from pathlib import Path


ALLOWED_LEVELS = ("off", "conservative", "balanced", "aggressive")
DEFAULT_LEVEL = "balanced"
ALLOWED_GOALS = ("balanced", "save-on-tokens", "parallel-review", "unrestricted")
DEFAULT_GOAL = "balanced"
ALLOWED_THINKING = ("default", "off", "minimal", "low", "medium", "high", "xhigh")
DEFAULT_THINKING = "default"
DEFAULT_TARGET_RESULT_CHARS = 12_000
DEFAULT_ARTIFACT_TTL_SECONDS = 3600
DEFAULT_ARTIFACT_ROOT = "~/.cache/local-agent-delegate/jobs"
RESULT_PROFILE = "artifact-backed-compact"
SESSION_BEHAVIOR = "per-job --session-dir under the artifact directory"
BACKEND_RESOURCE_MODE = (
    "context files enabled; extension discovery disabled; skill discovery disabled; "
    "bundled delegate scout skill loaded explicitly; prompt templates and themes disabled"
)

GUIDANCE = {
    "off": "Do not use delegation unless the user explicitly asks.",
    "conservative": "Use the backend for high-token read-only repo investigation when the question is clearly bounded.",
    "balanced": "Use the backend for token-heavy read-only exploration and comparisons; use patch mode for reviewable self-contained diffs.",
    "aggressive": "Prefer the backend for first-pass repo exploration, comparisons, audits, and candidate patches while still reviewing returned diffs.",
}

GOAL_GUIDANCE = {
    "balanced": "Use the backend when it is likely to save time or provide useful confidence; keep delegated tasks and returned results scoped.",
    "save-on-tokens": "Use the backend for token-heavy local exploration; after starting a delegated job, wait for the compact result before broad primary-agent CodeGraph, rg, find, ls, sed, or cat exploration.",
    "parallel-review": "Use the backend as an independent second opinion while the primary agent may continue local work; expect less token saving but better wall-clock overlap and confidence.",
    "unrestricted": "Let the backend explore broadly within the explicit task and safety rules; optimize for local-model work over primary-agent token economy.",
}

GOAL_WORKFLOW_GUIDANCE = {
    "balanced": "Delegate bounded exploration when useful, then verify the returned files or claims before relying on them.",
    "save-on-tokens": "Call local_agent_delegate_run_start, then local_agent_delegate_job_wait with include_details=false. Do not run broad primary-agent exploration until the delegated job returns a compact result or is clearly off-track.",
    "parallel-review": "Run the backend as an independent reviewer while the primary agent may continue useful work; compare conclusions before finalizing.",
    "unrestricted": "Let the backend perform broad local-model work within the explicit task and safety rules; keep final evidence review in the primary agent.",
}

LEVEL_REDELEGATION_GUIDANCE = {
    "off": "Do not re-delegate unless the user explicitly asks.",
    "conservative": "Re-delegate only for clearly bounded follow-up work that would otherwise require broad, high-token local inspection.",
    "balanced": "Re-delegate bounded follow-up exploration when a new subtask crosses the configured inspection threshold.",
    "aggressive": "Prefer re-delegating follow-up exploration whenever a new bounded subtask would otherwise consume primary-agent context.",
}

GOAL_REDELEGATION_GUIDANCE = {
    "balanced": "Use the threshold as a default; verify small follow-ups locally and re-delegate larger exploration phases.",
    "save-on-tokens": "Lower the threshold by one level and re-delegate follow-up exploration instead of doing broad primary-agent searches.",
    "parallel-review": "Use re-delegation for a separate second opinion, then compare conclusions locally rather than chaining many backend jobs.",
    "unrestricted": "Re-delegate freely for new bounded subtasks within the explicit task and safety rules.",
}

REDELEGATION_THRESHOLDS = {
    "off": "explicit user request only",
    "conservative": "6+ file reads/searches, cross-module tracing, or an independent second review",
    "balanced": "3+ file reads/searches or an unfamiliar subsystem",
    "aggressive": "2+ file reads/searches or any new subsystem after the first scout",
}

SAVE_ON_TOKENS_REDELEGATION_THRESHOLDS = {
    "off": REDELEGATION_THRESHOLDS["off"],
    "conservative": REDELEGATION_THRESHOLDS["balanced"],
    "balanced": REDELEGATION_THRESHOLDS["aggressive"],
    "aggressive": REDELEGATION_THRESHOLDS["aggressive"],
}

GOAL_VERIFICATION_BUDGET = {
    "balanced": "Verify the specific files, symbols, commands, or claims needed for confidence.",
    "save-on-tokens": "After the delegated job returns, verify only the named files, symbols, commands, or claims needed for correctness. If broad exploration is still needed, state that the backend was insufficient and why before continuing.",
    "parallel-review": "Use verification to resolve disagreements between the backend and the primary agent.",
    "unrestricted": "Verify safety-sensitive claims and any patch or command output before presenting conclusions.",
}

GOAL_SYSTEM_PROMPTS = {
    "balanced": (
        "Keep the final response focused on actionable findings, key file/function names, "
        "and important uncertainty. Avoid long excerpts unless they are necessary."
    ),
    "save-on-tokens": (
        "Optimize for reducing primary-agent token usage. Do the broad local inspection yourself, "
        "then return a compact answer with only decisive findings, file/function names, "
        "and suggested next checks. Avoid long code blocks, long quotes, and repeated context."
    ),
    "parallel-review": (
        "Act as an independent reviewer. Prioritize disagreements, uncertainty, and evidence "
        "that the primary agent should verify. Include enough detail to audit your reasoning, but do not dump files."
    ),
    "unrestricted": (
        "Explore broadly within the explicit task and available tools. Keep safety boundaries: "
        "do not touch secrets, commit, push, or modify files outside the requested workspace."
    ),
}


def current_level() -> str:
    raw = os.environ.get("LOCAL_AGENT_DELEGATE_LEAN", DEFAULT_LEVEL).strip().lower()
    return raw if raw in ALLOWED_LEVELS else DEFAULT_LEVEL


def current_goal() -> str:
    raw = os.environ.get("LOCAL_AGENT_DELEGATE_GOAL", DEFAULT_GOAL).strip().lower()
    return raw if raw in ALLOWED_GOALS else DEFAULT_GOAL


def current_thinking() -> str:
    raw = os.environ.get("LOCAL_AGENT_DELEGATE_THINKING", DEFAULT_THINKING).strip().lower()
    return raw if raw in ALLOWED_THINKING else DEFAULT_THINKING


def target_result_chars() -> int:
    raw = os.environ.get("LOCAL_AGENT_DELEGATE_TARGET_RESULT_CHARS", str(DEFAULT_TARGET_RESULT_CHARS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TARGET_RESULT_CHARS
    return value if value > 0 else DEFAULT_TARGET_RESULT_CHARS


def artifact_root() -> Path:
    raw = os.environ.get("LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT).strip() or DEFAULT_ARTIFACT_ROOT
    return Path(raw).expanduser().resolve()


def artifact_ttl_seconds() -> int:
    raw = os.environ.get("LOCAL_AGENT_DELEGATE_JOB_TTL_SECONDS", str(DEFAULT_ARTIFACT_TTL_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ARTIFACT_TTL_SECONDS
    return value if value > 0 else DEFAULT_ARTIFACT_TTL_SECONDS


def goal_system_prompt() -> str:
    return GOAL_SYSTEM_PROMPTS[current_goal()]


def redelegation_threshold(level: str, goal: str) -> str:
    if goal == "save-on-tokens":
        return SAVE_ON_TOKENS_REDELEGATION_THRESHOLDS[level]
    return REDELEGATION_THRESHOLDS[level]


def policy_summary() -> dict[str, str | int]:
    level = current_level()
    goal = current_goal()
    thinking = current_thinking()
    return {
        "level": level,
        "guidance": GUIDANCE[level],
        "allowed_levels": ", ".join(ALLOWED_LEVELS),
        "default_level": DEFAULT_LEVEL,
        "goal": goal,
        "goal_guidance": GOAL_GUIDANCE[goal],
        "workflow_guidance": GOAL_WORKFLOW_GUIDANCE[goal],
        "verification_budget": GOAL_VERIFICATION_BUDGET[goal],
        "level_redelegation_guidance": LEVEL_REDELEGATION_GUIDANCE[level],
        "goal_redelegation_guidance": GOAL_REDELEGATION_GUIDANCE[goal],
        "redelegation_threshold": redelegation_threshold(level, goal),
        "delegation_loop": "Scout -> verify named evidence -> before each new exploration phase, compare expected reads/searches to redelegation_threshold -> re-delegate if the threshold is met.",
        "allowed_goals": ", ".join(ALLOWED_GOALS),
        "default_goal": DEFAULT_GOAL,
        "thinking": thinking,
        "thinking_guidance": "default means Local Agent Delegate does not pass --thinking; other values are passed to pi --thinking.",
        "allowed_thinking": ", ".join(ALLOWED_THINKING),
        "default_thinking": DEFAULT_THINKING,
        "result_profile": RESULT_PROFILE,
        "target_result_chars": target_result_chars(),
        "artifact_root": str(artifact_root()),
        "artifact_ttl_seconds": artifact_ttl_seconds(),
        "session_behavior": SESSION_BEHAVIOR,
        "backend_resource_mode": BACKEND_RESOURCE_MODE,
    }
