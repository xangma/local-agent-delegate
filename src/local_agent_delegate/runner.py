from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .policy import DEFAULT_THINKING, current_thinking


DEFAULT_MODEL: str | None = None
READ_ONLY_TOOLS = "read,grep,find,ls"
PATCH_TOOLS = "read,grep,find,ls,bash,edit,write"
DEFAULT_TIMEOUT = 300
BACKEND_INSTALL_HINT = (
    "Install Pi, ensure `pi --version` works, configure `~/.pi/agent/models.json`, "
    "and either put `pi` on PATH or set LOCAL_AGENT_DELEGATE_PI_BIN=/path/to/pi."
)
PROGRESS_SYSTEM_PROMPT = (
    "When running non-interactively for an MCP client, emit brief progress lines prefixed exactly "
    "`PI_PROGRESS:` before notable work that is not already obvious from tool calls, such as "
    "choosing an approach, comparing files, editing files, or running checks. Keep each progress "
    "line short and concrete, and include paths or commands when useful."
)


class LocalAgentDelegateError(Exception):
    """Raised for user-facing local-agent-delegate failures."""


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def short_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


def resolve_cwd(raw_cwd: str | None) -> Path:
    path = Path(raw_cwd or os.getcwd()).expanduser().resolve()
    if not path.exists():
        raise LocalAgentDelegateError(f"cwd does not exist: {path}")
    if not path.is_dir():
        raise LocalAgentDelegateError(f"cwd is not a directory: {path}")
    return path


def pi_executable() -> str:
    explicit = os.environ.get("LOCAL_AGENT_DELEGATE_PI_BIN")
    if explicit:
        return explicit
    resolved = shutil.which("pi")
    if not resolved:
        raise LocalAgentDelegateError("pi executable not found on PATH")
    return resolved


def default_model() -> str | None:
    raw = os.environ.get("LOCAL_AGENT_DELEGATE_MODEL")
    if raw is None:
        return DEFAULT_MODEL
    return raw.strip() or DEFAULT_MODEL


def thinking_args(thinking: str | None = None) -> list[str]:
    chosen = current_thinking() if thinking is None else thinking
    if chosen == DEFAULT_THINKING:
        return []
    return ["--thinking", chosen]


def pi_subprocess_env(executable: str) -> dict[str, str]:
    env = os.environ.copy()
    paths = []
    node_bin = env.get("LOCAL_AGENT_DELEGATE_NODE_BIN")
    if node_bin:
        paths.append(str(Path(node_bin).expanduser().resolve().parent))
    executable_path = Path(executable).expanduser()
    if executable_path.is_absolute():
        paths.append(str(executable_path.parent))
    elif executable_path.parent != Path("."):
        paths.append(str(executable_path.parent.resolve()))
    if paths:
        existing = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([*dedupe(paths), existing] if existing else dedupe(paths))
    return env


def dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def run_command(command: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> CommandResult:
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalAgentDelegateError(f"command timed out after {timeout}s: {' '.join(command)}") from exc
    return CommandResult(
        command=command,
        cwd=str(cwd),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def status(model: str | None = None, timeout: int = 30) -> dict[str, Any]:
    try:
        executable = pi_executable()
    except LocalAgentDelegateError as exc:
        return {
            "available": False,
            "reason": "backend_not_found",
            "message": str(exc),
            "install_hint": BACKEND_INSTALL_HINT,
            "configured_backend_bin": os.environ.get("LOCAL_AGENT_DELEGATE_PI_BIN"),
            "default_model": default_model(),
            "thinking": current_thinking(),
        }
    env = pi_subprocess_env(executable)
    try:
        version = run_command([executable, "--version"], Path.home(), timeout, env=env)
    except (OSError, LocalAgentDelegateError) as exc:
        return {
            "available": False,
            "reason": "backend_version_failed",
            "message": str(exc),
            "install_hint": BACKEND_INSTALL_HINT,
            "backend_executable": executable,
            "configured_backend_bin": os.environ.get("LOCAL_AGENT_DELEGATE_PI_BIN"),
            "default_model": default_model(),
            "thinking": current_thinking(),
        }
    models_args = [executable, "--list-models"]
    if model:
        models_args.append(model)
    try:
        models = run_command(models_args, Path.home(), timeout, env=env)
    except (OSError, LocalAgentDelegateError) as exc:
        return {
            "available": False,
            "reason": "backend_models_failed",
            "message": str(exc),
            "install_hint": BACKEND_INSTALL_HINT,
            "backend_executable": executable,
            "version": version.as_dict(),
            "configured_backend_bin": os.environ.get("LOCAL_AGENT_DELEGATE_PI_BIN"),
            "default_model": default_model(),
            "thinking": current_thinking(),
        }
    return {
        "available": version.returncode == 0 and models.returncode == 0,
        "reason": "ok" if version.returncode == 0 and models.returncode == 0 else "backend_probe_failed",
        "install_hint": BACKEND_INSTALL_HINT,
        "backend_executable": executable,
        "version": version.as_dict(),
        "models": models.as_dict(),
        "default_model": default_model(),
        "thinking": current_thinking(),
    }


def patch_prompt(prompt: str) -> str:
    return (
        "You are running in a disposable git worktree created for the calling agent. "
        "Make only the smallest coherent patch requested. Do not commit, push, or modify files outside this worktree. "
        "When done, summarize changed files and tests or checks you ran.\n\n"
        f"Task:\n{prompt}"
    )


def git_output(command: list[str], allow_failure: bool = False) -> str:
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode != 0 and not allow_failure:
        raise LocalAgentDelegateError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}")
    return proc.stdout


def ensure_clean(repo_root: Path) -> None:
    status_text = git_output(["git", "-C", str(repo_root), "status", "--short"])
    if status_text.strip():
        raise LocalAgentDelegateError(
            "patch mode requires a clean git worktree so Pi sees the same baseline the caller will review"
        )
