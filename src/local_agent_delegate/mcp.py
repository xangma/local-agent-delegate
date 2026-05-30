from __future__ import annotations

import json
import os
import sys
from typing import Any

from .jobs import JobManager
from .policy import ALLOWED_THINKING, policy_summary
from .runner import LocalAgentDelegateError, status


SERVER_INFO = {"name": "local-agent-delegate", "version": "0.3.0"}
JSON_RPC_VERSION = "2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MAX_TOOL_TEXT = 160_000
MAX_WAIT_TIMEOUT = 90
JOBS = JobManager()
THINKING_SCHEMA = {"type": "string", "enum": list(ALLOWED_THINKING), "default": "default"}
MODEL_SCHEMA = {"type": "string", "description": "Optional backend model name; omit to use the local agent's configured default."}


TOOLS = [
    {
        "name": "local_agent_delegate_policy",
        "description": "Return the configured delegation level/goal and re-delegation threshold from the MCP server env; use this instead of shell printenv.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_status",
        "description": "Check local agent backend installation and configured model availability, returning install hints instead of failing when the backend is missing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Optional model filter for pi --list-models."},
                "timeout": {"type": "integer", "minimum": 1, "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_run_start",
        "description": "Start a long-running read-only local-agent job and return immediately with a job id. Use again for distinct follow-up exploration phases when the policy threshold is met. When the goal is save-on-tokens, wait for the compact result before broad local exploration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1},
                "cwd": {"type": "string", "description": "Local directory where the backend should run."},
                "model": MODEL_SCHEMA,
                "timeout": {"type": "integer", "minimum": 1, "default": 300},
                "thinking": THINKING_SCHEMA,
            },
            "required": ["prompt", "cwd"],
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_patch_start",
        "description": "Start a long-running local-agent patch job in a disposable worktree and return immediately with a job id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1},
                "cwd": {"type": "string", "description": "Local git repo directory to branch from."},
                "model": MODEL_SCHEMA,
                "timeout": {"type": "integer", "minimum": 1, "default": 300},
                "base_ref": {"type": "string", "default": "HEAD"},
                "thinking": THINKING_SCHEMA,
            },
            "required": ["prompt", "cwd"],
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_job_status",
        "description": "Return status for a previously started delegated job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "minLength": 1},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_job_result",
        "description": "Fetch the compact final result for a delegated job; details are compact diagnostics and artifact paths only. Results may have result_state=partial_timeout when the backend timed out after producing useful text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "minLength": 1},
                "include_details": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include compact diagnostics; never includes raw stdout/stderr/event tails.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_job_wait",
        "description": "Wait for a delegated job to complete or for a bounded timeout, returning compact result/status. Completed jobs may return result_state=partial_timeout when the backend timed out after producing useful text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "minLength": 1},
                "wait_timeout": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_TIMEOUT, "default": 90},
                "include_details": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include compact diagnostics; never includes raw stdout/stderr/event tails.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_job_cancel",
        "description": "Cancel a running delegated job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "minLength": 1},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "local_agent_delegate_jobs",
        "description": "List retained delegated jobs in this MCP server process.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def handle_tool(name: str, args: dict[str, Any]) -> Any:
    if name == "local_agent_delegate_policy":
        return policy_summary()
    if name == "local_agent_delegate_status":
        return status(model=optional_string(args, "model"), timeout=int_arg(args, "timeout", 30))
    if name == "local_agent_delegate_run":
        raise LocalAgentDelegateError(
            "local_agent_delegate_run has been removed; use local_agent_delegate_run_start, "
            "then poll local_agent_delegate_job_status and fetch local_agent_delegate_job_result"
        )
    if name == "local_agent_delegate_patch":
        raise LocalAgentDelegateError(
            "local_agent_delegate_patch has been removed; use local_agent_delegate_patch_start, "
            "then poll local_agent_delegate_job_status and fetch local_agent_delegate_job_result"
        )
    if name == "local_agent_delegate_run_start":
        return JOBS.start_read_only(
            prompt=required_string(args, "prompt"),
            cwd=required_string(args, "cwd"),
            model=optional_string(args, "model"),
            timeout=int_arg(args, "timeout", 300),
            thinking=optional_thinking(args, "thinking"),
        )
    if name == "local_agent_delegate_patch_start":
        return JOBS.start_patch(
            prompt=required_string(args, "prompt"),
            cwd=required_string(args, "cwd"),
            model=optional_string(args, "model"),
            timeout=int_arg(args, "timeout", 300),
            base_ref=optional_string(args, "base_ref") or "HEAD",
            thinking=optional_thinking(args, "thinking"),
        )
    if name == "local_agent_delegate_job_status":
        return JOBS.get_status(required_string(args, "job_id"))
    if name == "local_agent_delegate_job_result":
        return JOBS.get_result(required_string(args, "job_id"), include_details=bool_arg(args, "include_details", False))
    if name == "local_agent_delegate_job_wait":
        return JOBS.wait(
            required_string(args, "job_id"),
            wait_timeout=int_arg(args, "wait_timeout", 90, maximum=MAX_WAIT_TIMEOUT),
            include_details=bool_arg(args, "include_details", False),
        )
    if name == "local_agent_delegate_job_cancel":
        return JOBS.cancel(required_string(args, "job_id"))
    if name == "local_agent_delegate_jobs":
        return JOBS.list_jobs()
    raise LocalAgentDelegateError(f"unknown tool: {name}")


def required_string(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LocalAgentDelegateError(f"missing required string argument: {key}")
    return value.strip()


def optional_string(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise LocalAgentDelegateError(f"{key} must be a string")
    return value.strip() or None


def int_arg(args: dict[str, Any], key: str, fallback: int, *, maximum: int | None = None) -> int:
    value = args.get(key, fallback)
    if not isinstance(value, int) or value <= 0:
        raise LocalAgentDelegateError(f"{key} must be a positive integer")
    if maximum is not None and value > maximum:
        raise LocalAgentDelegateError(f"{key} must be between 1 and {maximum}")
    return value


def bool_arg(args: dict[str, Any], key: str, fallback: bool) -> bool:
    value = args.get(key, fallback)
    if not isinstance(value, bool):
        raise LocalAgentDelegateError(f"{key} must be a boolean")
    return value


def optional_thinking(args: dict[str, Any], key: str) -> str | None:
    value = optional_string(args, key)
    if value is None:
        return None
    if value not in ALLOWED_THINKING:
        raise LocalAgentDelegateError(f"{key} must be one of: {', '.join(ALLOWED_THINKING)}")
    return value


def tool_content(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)
    if len(text) > MAX_TOOL_TEXT:
        text = json.dumps(
            {
                "error": "tool_response_oversized",
                "message": "tool response exceeded MCP text budget; no partial payload returned",
                "actual_chars": len(text),
                "max_chars": MAX_TOOL_TEXT,
            },
            indent=2,
            sort_keys=True,
        )
    return {"content": [{"type": "text", "text": text}]}


def handle_request(message: dict[str, Any]) -> Any:
    method = message.get("method")
    params = message.get("params") or {}
    if message.get("id") is None:
        return None
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            return tool_content(handle_tool(params.get("name", ""), params.get("arguments") or {}))
        except Exception as exc:  # noqa: BLE001 - MCP surfaces tool failures as tool content.
            return {**tool_content(str(exc)), "isError": True}
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    raise JsonRpcError(-32601, f"Method not found: {method}")


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def write_message(message: dict[str, Any]) -> None:
    payload = json.dumps(message)
    if os.environ.get("MCP_STDIO_FRAMING") == "headers":
        sys.stdout.write(f"Content-Length: {len(payload.encode('utf-8'))}\r\n\r\n{payload}")
    else:
        sys.stdout.write(f"{payload}\n")
    sys.stdout.flush()


def send_response(message_id: Any, result: Any) -> None:
    write_message({"jsonrpc": JSON_RPC_VERSION, "id": message_id, "result": result})


def send_error(message_id: Any, error: Exception) -> None:
    write_message(
        {
            "jsonrpc": JSON_RPC_VERSION,
            "id": message_id,
            "error": {
                "code": error.code if isinstance(error, JsonRpcError) else -32603,
                "message": str(error),
            },
        }
    )


def dispatch(raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        send_error(None, JsonRpcError(-32700, f"Parse error: {exc}"))
        return
    try:
        result = handle_request(message)
    except Exception as exc:  # noqa: BLE001
        send_error(message.get("id"), exc)
        return
    if result is not None:
        send_response(message.get("id"), result)


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if raw:
            dispatch(raw)


if __name__ == "__main__":
    main()
