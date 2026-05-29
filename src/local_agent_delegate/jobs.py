from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
import uuid

from .policy import artifact_root, artifact_ttl_seconds, current_thinking, goal_system_prompt, target_result_chars
from .runner import (
    DEFAULT_TIMEOUT,
    PATCH_TOOLS,
    PROGRESS_SYSTEM_PROMPT,
    READ_ONLY_TOOLS,
    LocalAgentDelegateError,
    default_model,
    ensure_clean,
    git_output,
    patch_prompt,
    pi_executable,
    pi_subprocess_env,
    resolve_cwd,
    run_command,
    short_text,
    thinking_args,
)


MAX_JOBS = 32
TERMINATE_GRACE_SECONDS = 5
MAX_ACTIVITY = 80
ACTIVITY_TAIL = 20
PROGRESS_PREFIX = "PI_PROGRESS:"
DEFAULT_WAIT_TIMEOUT = 90
COMPACTION_TIMEOUT = 120
JSON_PARSE_CHAR_LIMIT = 64_000
APPROX_BYTES_PER_TOKEN = 4
MINIMAL_COUNTER_KEYS = (
    "total_stream_bytes",
    "delivered_assistant_chars",
    "estimated_primary_tokens_avoided_approx",
    "estimation_bytes_per_token",
)
ASSISTANT_UPDATE_TYPE_RE = re.compile(r'"assistantMessageEvent"\s*:\s*\{[^{}]*"type"\s*:\s*"([^"]+)"')


def delegate_system_prompt() -> str:
    return f"{PROGRESS_SYSTEM_PROMPT}\n\n{goal_system_prompt()}"


class JobCancelled(Exception):
    """Raised inside worker threads when cancellation is requested."""


@dataclass
class AgentProcessResult:
    command: list[str]
    returncode: int


@dataclass
class CaptureTarget:
    events_path: Path
    assistant_path: Path
    stderr_path: Path
    stdout_bytes_field: str
    stderr_bytes_field: str
    json_events_field: str
    record_activity: bool = True
    record_errors: bool = True


@dataclass
class DelegateJob:
    job_id: str
    mode: str
    cwd: str
    model: str | None
    thinking: str
    timeout: int
    prompt_preview: str
    artifact_dir: Path
    session_dir: Path
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    state: str = "running"
    result: dict[str, Any] | None = None
    error: str | None = None
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    cancel_requested: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)
    stdout_line_buffer: str = field(default="", repr=False)
    assistant_line_buffer: str = field(default="", repr=False)
    compact_stdout_line_buffer: str = field(default="", repr=False)
    compact_assistant_line_buffer: str = field(default="", repr=False)
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    assistant_chars: int = 0
    json_events: int = 0
    compact_stdout_bytes: int = 0
    compact_stderr_bytes: int = 0
    compact_assistant_chars: int = 0
    compact_json_events: int = 0
    activity: list[dict[str, Any]] = field(default_factory=list, repr=False)
    running_tools: dict[str, str] = field(default_factory=dict, repr=False)
    backend_message_error: str | None = field(default=None, repr=False)
    backend_terminal_error: str | None = field(default=None, repr=False)
    session_id: str | None = None
    session_file: str | None = None
    last_output_at: float | None = None
    last_activity_at: float | None = None
    backend_returncode: int | None = None

    @property
    def events_path(self) -> Path:
        return self.artifact_dir / "events.jsonl"

    @property
    def assistant_path(self) -> Path:
        return self.artifact_dir / "assistant.txt"

    @property
    def stderr_path(self) -> Path:
        return self.artifact_dir / "stderr.txt"

    @property
    def activity_path(self) -> Path:
        return self.artifact_dir / "activity.json"

    @property
    def diff_path(self) -> Path:
        return self.artifact_dir / "diff.patch"

    @property
    def compact_events_path(self) -> Path:
        return self.artifact_dir / "compact-events.jsonl"

    @property
    def compact_assistant_path(self) -> Path:
        return self.artifact_dir / "compact-assistant.txt"

    @property
    def compact_stderr_path(self) -> Path:
        return self.artifact_dir / "compact-stderr.txt"


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, DelegateJob] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._prune_artifacts()

    def start_read_only(
        self,
        *,
        prompt: str,
        cwd: str | None,
        model: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        thinking: str | None = None,
    ) -> dict[str, Any]:
        workdir = resolve_cwd(cwd)
        chosen_model = model or default_model()
        chosen_thinking = current_thinking() if thinking is None else thinking
        return self._start(
            mode="read_only",
            cwd=str(workdir),
            model=chosen_model,
            thinking=chosen_thinking,
            timeout=timeout,
            prompt=prompt,
            target=lambda job: self._run_read_only(job, workdir, prompt),
        )

    def start_patch(
        self,
        *,
        prompt: str,
        cwd: str | None,
        model: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        base_ref: str = "HEAD",
        thinking: str | None = None,
    ) -> dict[str, Any]:
        repo = resolve_cwd(cwd)
        repo_root = git_output(["git", "-C", str(repo), "rev-parse", "--show-toplevel"]).strip()
        root = Path(repo_root).resolve()
        ensure_clean(root)
        chosen_model = model or default_model()
        chosen_thinking = current_thinking() if thinking is None else thinking
        return self._start(
            mode="patch",
            cwd=str(root),
            model=chosen_model,
            thinking=chosen_thinking,
            timeout=timeout,
            prompt=prompt,
            target=lambda job: self._run_patch(job, root, base_ref, prompt),
        )

    def get_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(self._get_job_locked(job_id), include_details=True)

    def get_result(self, job_id: str, *, include_details: bool = False) -> dict[str, Any]:
        with self._lock:
            job = self._get_job_locked(job_id)
            snapshot = self._snapshot_locked(job, include_details=include_details)
            if job.state in {"running", "cancelling"}:
                return {**snapshot, "ready": False}
            if job.result is not None:
                return {**snapshot, "ready": True, "result": result_for_response(job, include_details=include_details)}
            return {**snapshot, "ready": True}

    def wait(self, job_id: str, *, wait_timeout: int = DEFAULT_WAIT_TIMEOUT, include_details: bool = False) -> dict[str, Any]:
        deadline = time.time() + wait_timeout
        with self._condition:
            job = self._get_job_locked(job_id)
            while job.state in {"running", "cancelling"}:
                remaining = deadline - time.time()
                if remaining <= 0:
                    snapshot = self._snapshot_locked(job, include_details=include_details)
                    return {**snapshot, "ready": False, "timed_out": True}
                self._condition.wait(timeout=min(remaining, 0.25))
            snapshot = self._snapshot_locked(job, include_details=include_details)
            if job.result is not None:
                return {
                    **snapshot,
                    "ready": True,
                    "timed_out": False,
                    "result": result_for_response(job, include_details=include_details),
                }
            return {**snapshot, "ready": True, "timed_out": False}

    def cancel(self, job_id: str) -> dict[str, Any]:
        proc: subprocess.Popen[str] | None = None
        with self._condition:
            job = self._get_job_locked(job_id)
            if job.state not in {"running", "cancelling"}:
                return self._snapshot_locked(job)
            job.cancel_requested = True
            job.state = "cancelling"
            proc = job.process
            self._condition.notify_all()
        if proc is not None:
            terminate_process(proc)
        with self._lock:
            return self._snapshot_locked(job)

    def list_jobs(self) -> dict[str, Any]:
        self._prune()
        with self._lock:
            jobs = [self._snapshot_locked(job, include_details=False) for job in self._jobs.values()]
        jobs.sort(key=lambda item: item["started_at"], reverse=True)
        return {"jobs": jobs}

    def _start(
        self,
        *,
        mode: str,
        cwd: str,
        model: str | None,
        thinking: str,
        timeout: int,
        prompt: str,
        target: Callable[[DelegateJob], dict[str, Any]],
    ) -> dict[str, Any]:
        self._prune()
        with self._lock:
            if len(self._jobs) >= MAX_JOBS:
                raise LocalAgentDelegateError("too many local-agent-delegate jobs retained; fetch or wait for older jobs first")
        job_id = uuid.uuid4().hex
        artifact_dir = create_artifact_dir(job_id)
        try:
            session_dir = artifact_dir / "session"
            session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            job = DelegateJob(
                job_id=job_id,
                mode=mode,
                cwd=cwd,
                model=model,
                thinking=thinking,
                timeout=timeout,
                prompt_preview=short_text(prompt.replace("\n", " "), 240),
                artifact_dir=artifact_dir,
                session_dir=session_dir,
            )
            initialize_artifacts(job)
            thread = threading.Thread(target=self._worker, args=(job, target), daemon=True)
            job.thread = thread
        except Exception:
            remove_tree(artifact_dir)
            raise
        with self._lock:
            if len(self._jobs) >= MAX_JOBS:
                remove_tree(artifact_dir)
                raise LocalAgentDelegateError("too many local-agent-delegate jobs retained; fetch or wait for older jobs first")
            self._jobs[job.job_id] = job
        thread.start()
        with self._lock:
            return {
                **self._snapshot_locked(job, include_details=False),
                "message": "poll with local_agent_delegate_job_status, wait with local_agent_delegate_job_wait, or fetch local_agent_delegate_job_result",
            }

    def _worker(self, job: DelegateJob, target: Callable[[DelegateJob], dict[str, Any]]) -> None:
        try:
            result = target(job)
            with self._condition:
                if job.cancel_requested:
                    job.state = "cancelled"
                    job.error = "job cancelled"
                elif job.backend_terminal_error:
                    if result_has_complete_text(result):
                        job.state = "succeeded"
                        result["recovered_error"] = job.backend_terminal_error
                        job.result = result
                    else:
                        job.state = "failed"
                        job.error = f"backend reported error: {job.backend_terminal_error}"
                        job.result = result
                elif backend_returncode(result) != 0:
                    job.state = "failed"
                    job.error = f"backend exited with return code {backend_returncode(result)}"
                    job.result = result
                else:
                    job.state = "succeeded"
                    job.result = result
                job.completed_at = time.time()
                job.process = None
                self._write_activity_locked(job)
                self._condition.notify_all()
        except JobCancelled:
            with self._condition:
                job.state = "cancelled"
                job.error = "job cancelled"
                job.completed_at = time.time()
                job.process = None
                self._write_activity_locked(job)
                self._condition.notify_all()
        except Exception as exc:  # noqa: BLE001 - errors are returned through MCP result polling.
            with self._condition:
                job.state = "failed"
                job.error = str(exc)
                job.completed_at = time.time()
                job.process = None
                self._write_activity_locked(job)
                self._condition.notify_all()

    def _run_read_only(self, job: DelegateJob, workdir: Path, prompt: str) -> dict[str, Any]:
        command = self._pi_command(job, READ_ONLY_TOOLS, prompt)
        result = self._run_process(job, command, workdir, self._main_capture(job), timeout=job.timeout)
        job.backend_returncode = result.returncode
        text_result = self._result_text(job, workdir)
        return {
            "type": "read_only",
            "result_state": text_result["result_state"],
            "text": text_result["text"],
            "target_chars": target_result_chars(),
            "artifacts": artifact_paths(job),
            "backend_returncode": result.returncode,
        }

    def _run_patch(self, job: DelegateJob, root: Path, base_ref: str, prompt: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="local-agent-delegate-worktree-") as tmp:
            worktree = Path(tmp) / "repo"
            add = run_command(["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), base_ref], root, 60)
            if add.returncode != 0:
                raise LocalAgentDelegateError(f"git worktree add failed: {add.stderr or add.stdout}")
            try:
                command = self._pi_command(job, PATCH_TOOLS, patch_prompt(prompt))
                pi_result = self._run_process(job, command, worktree, self._main_capture(job), timeout=job.timeout)
                job.backend_returncode = pi_result.returncode
                if job.cancel_requested:
                    raise JobCancelled()
                diff = git_output(["git", "-C", str(worktree), "diff", "--no-ext-diff"])
                job.diff_path.write_text(diff, encoding="utf-8")
                status_text = git_output(["git", "-C", str(worktree), "status", "--short"], allow_failure=True)
                shortstat = git_output(["git", "-C", str(worktree), "diff", "--shortstat", "--no-ext-diff"], allow_failure=True)
                text_result = self._result_text(job, worktree)
                return {
                    "type": "patch",
                    "result_state": text_result["result_state"],
                    "summary_text": text_result["text"],
                    "target_chars": target_result_chars(),
                    "source_repo": str(root),
                    "base_ref": base_ref,
                    "backend_returncode": pi_result.returncode,
                    "diff_artifact": str(job.diff_path),
                    "diff_stats": {
                        "bytes": job.diff_path.stat().st_size,
                        "shortstat": shortstat.strip(),
                        "git_status": status_text.strip(),
                    },
                    "artifacts": artifact_paths(job),
                    "applied_to_current_tree": False,
                }
            finally:
                run_command(["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)], root, 60)

    def _pi_command(self, job: DelegateJob, tools: str, prompt: str) -> list[str]:
        return [
            pi_executable(),
            *thinking_args(job.thinking),
            "--mode",
            "json",
            "--session-dir",
            str(job.session_dir),
            "--append-system-prompt",
            delegate_system_prompt(),
            *model_args(job.model),
            "--no-extensions",
            "--no-skills",
            "--skill",
            str(delegate_scout_skill_path()),
            "--no-prompt-templates",
            "--no-themes",
            "--tools",
            tools,
            "-p",
            prompt,
        ]

    def _compaction_command(self, job: DelegateJob, source_path: Path) -> list[str]:
        compact_session_dir = job.artifact_dir / "compact-session"
        compact_session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        prompt = (
            f"Rewrite the attached assistant result into at most {target_result_chars()} characters. "
            "Preserve concrete findings, file paths, commands, errors, and next steps. "
            "Return only the compact result text."
        )
        return [
            pi_executable(),
            *thinking_args(job.thinking),
            "--mode",
            "json",
            "--session-dir",
            str(compact_session_dir),
            *model_args(job.model),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-tools",
            "-p",
            f"@{source_path}",
            prompt,
        ]

    def _run_process(
        self,
        job: DelegateJob,
        command: list[str],
        cwd: Path,
        capture: CaptureTarget,
        *,
        timeout: int,
    ) -> AgentProcessResult:
        if job.cancel_requested:
            raise JobCancelled()
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=pi_subprocess_env(command[0]),
        )
        capture_threads = [
            threading.Thread(target=self._capture_stdout, args=(job, proc.stdout, capture), daemon=True),
            threading.Thread(target=self._capture_stderr, args=(job, proc.stderr, capture), daemon=True),
        ]
        with self._lock:
            job.process = proc
            if job.cancel_requested:
                terminate_process(proc)
        for thread in capture_threads:
            thread.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process(proc, kill=True)
            try:
                proc.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            raise LocalAgentDelegateError(f"command timed out after {timeout}s: {command_summary(command)}") from exc
        finally:
            for thread in capture_threads:
                thread.join(timeout=1)
            with self._lock:
                self._flush_stdout_buffer_locked(job, capture)
                self._flush_assistant_buffer_locked(job, capture)
                if job.process is proc:
                    job.process = None
        if job.cancel_requested:
            raise JobCancelled()
        return AgentProcessResult(command=command, returncode=proc.returncode)

    def _capture_stdout(self, job: DelegateJob, stream: Any, capture: CaptureTarget) -> None:
        if stream is None:
            return
        try:
            with capture.events_path.open("a", encoding="utf-8") as file:
                for line in stream:
                    file.write(line)
                    file.flush()
                    with self._lock:
                        setattr(
                            job,
                            capture.stdout_bytes_field,
                            getattr(job, capture.stdout_bytes_field) + len(line.encode("utf-8")),
                        )
                        job.last_output_at = time.time()
                        self._ingest_stdout_line_locked(job, line.rstrip("\n"), capture)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _capture_stderr(self, job: DelegateJob, stream: Any, capture: CaptureTarget) -> None:
        if stream is None:
            return
        try:
            with capture.stderr_path.open("a", encoding="utf-8") as file:
                while True:
                    chunk = stream.read(8192)
                    if chunk == "":
                        break
                    file.write(chunk)
                    file.flush()
                    with self._lock:
                        setattr(
                            job,
                            capture.stderr_bytes_field,
                            getattr(job, capture.stderr_bytes_field) + len(chunk.encode("utf-8")),
                        )
                        job.last_output_at = time.time()
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _ingest_stdout_line_locked(self, job: DelegateJob, line: str, capture: CaptureTarget) -> None:
        if len(line) > JSON_PARSE_CHAR_LIMIT and self._handle_large_stdout_line_locked(job, line, capture):
            setattr(job, capture.json_events_field, getattr(job, capture.json_events_field) + 1)
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if not line.lstrip().startswith("{"):
                self._append_assistant_text_locked(job, f"{line}\n", capture)
            return
        if not isinstance(event, dict):
            return
        setattr(job, capture.json_events_field, getattr(job, capture.json_events_field) + 1)
        self._handle_pi_event_locked(job, event, capture)

    def _handle_large_stdout_line_locked(self, job: DelegateJob, line: str, capture: CaptureTarget) -> bool:
        event_type = json_string_from_line(line, "type")
        if event_type is None:
            return False
        self._maybe_record_session_from_line_locked(job, line)
        if event_type == "tool_execution_end":
            if capture.record_activity:
                tool_call_id = json_string_from_line(line, "toolCallId") or ""
                summary = job.running_tools.pop(tool_call_id, "") if tool_call_id else ""
                if json_bool_from_line(line, "isError"):
                    self._record_activity_locked(job, "tool_error", f"{summary or 'tool'} failed")
            return True
        if event_type == "message_update":
            update_match = ASSISTANT_UPDATE_TYPE_RE.search(line)
            update_type = update_match.group(1) if update_match else None
            return update_type != "text_delta"
        if event_type == "tool_execution_start":
            if capture.record_activity:
                tool_call_id = json_string_from_line(line, "toolCallId") or ""
                tool_name = json_string_from_line(line, "toolName") or "tool"
                if tool_call_id:
                    job.running_tools[tool_call_id] = tool_name
                self._record_activity_locked(job, "tool_start", tool_name)
            return True
        return False

    def _flush_stdout_buffer_locked(self, job: DelegateJob, capture: CaptureTarget) -> None:
        buffer_name = "compact_stdout_line_buffer" if capture.events_path == job.compact_events_path else "stdout_line_buffer"
        line = getattr(job, buffer_name)
        if line:
            self._ingest_stdout_line_locked(job, line, capture)
            setattr(job, buffer_name, "")

    def _handle_pi_event_locked(self, job: DelegateJob, event: dict[str, Any], capture: CaptureTarget) -> None:
        self._maybe_record_session_locked(job, event)
        event_type = event.get("type")
        if event_type in {"message_start", "message_end", "turn_end"}:
            error = message_error(event.get("message"))
            if error and capture.record_errors:
                job.backend_message_error = error
            if event_type in {"message_end", "turn_end"}:
                text = assistant_text_from_message(event.get("message"))
                if text is not None:
                    self._replace_assistant_text_locked(job, text, capture)
            return
        if event_type == "agent_end":
            if event.get("willRetry") is True:
                return
            error = messages_error(event.get("messages"))
            if error is None and "messages" not in event:
                error = job.backend_message_error
            if error and capture.record_errors:
                job.backend_terminal_error = error
            return
        if event_type == "auto_retry_end":
            if event.get("success") is False and capture.record_errors:
                job.backend_terminal_error = string_value(event, "finalError") or job.backend_message_error or "retry attempts failed"
            elif event.get("success") is True and capture.record_errors:
                job.backend_message_error = None
            return
        if event_type == "error":
            if capture.record_errors:
                job.backend_terminal_error = string_value(event, "message") or string_value(event, "error") or "unknown backend error"
            return
        if event_type == "tool_execution_start" and capture.record_activity:
            tool_call_id = str(event.get("toolCallId") or "")
            summary = summarize_tool_action(str(event.get("toolName") or "tool"), event.get("args"))
            if tool_call_id:
                job.running_tools[tool_call_id] = summary
            self._record_activity_locked(job, "tool_start", summary)
            return
        if event_type == "tool_execution_end" and capture.record_activity:
            tool_call_id = str(event.get("toolCallId") or "")
            summary = job.running_tools.pop(tool_call_id, "") if tool_call_id else ""
            if event.get("isError"):
                self._record_activity_locked(job, "tool_error", f"{summary or 'tool'} failed")
            return
        if event_type == "message_update":
            update = event.get("assistantMessageEvent")
            if isinstance(update, dict) and update.get("type") == "text_delta":
                self._append_assistant_text_locked(job, str(update.get("delta") or ""), capture)
            return

    def _maybe_record_session_locked(self, job: DelegateJob, event: dict[str, Any]) -> None:
        session = event.get("session")
        if isinstance(session, dict):
            job.session_id = string_value(session, "id") or job.session_id
            job.session_file = string_value(session, "path") or string_value(session, "file") or job.session_file
        for key in ("sessionId", "session_id"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                job.session_id = value.strip()
        for key in ("sessionPath", "sessionFile", "session_file"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                job.session_file = value.strip()

    def _maybe_record_session_from_line_locked(self, job: DelegateJob, line: str) -> None:
        job.session_id = json_string_from_line(line, "sessionId") or json_string_from_line(line, "session_id") or job.session_id
        job.session_file = (
            json_string_from_line(line, "sessionPath")
            or json_string_from_line(line, "sessionFile")
            or json_string_from_line(line, "session_file")
            or job.session_file
        )

    def _append_assistant_text_locked(self, job: DelegateJob, delta: str, capture: CaptureTarget) -> None:
        if not delta:
            return
        buffer_name = "compact_assistant_line_buffer" if capture.assistant_path == job.compact_assistant_path else "assistant_line_buffer"
        buffer = getattr(job, buffer_name) + delta
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            self._write_assistant_line_locked(job, line, capture)
        setattr(job, buffer_name, buffer)

    def _flush_assistant_buffer_locked(self, job: DelegateJob, capture: CaptureTarget) -> None:
        buffer_name = "compact_assistant_line_buffer" if capture.assistant_path == job.compact_assistant_path else "assistant_line_buffer"
        line = getattr(job, buffer_name)
        if line:
            self._write_assistant_line_locked(job, line, capture)
            setattr(job, buffer_name, "")

    def _write_assistant_line_locked(self, job: DelegateJob, line: str, capture: CaptureTarget) -> None:
        clean = line.strip()
        if clean.startswith(PROGRESS_PREFIX):
            if capture.record_activity:
                self._record_activity_locked(job, "progress", clean[len(PROGRESS_PREFIX) :].strip())
            return
        with capture.assistant_path.open("a", encoding="utf-8") as file:
            written = f"{line}\n"
            file.write(written)
        if capture.assistant_path == job.compact_assistant_path:
            job.compact_assistant_chars += len(written)
        else:
            job.assistant_chars += len(written)

    def _replace_assistant_text_locked(self, job: DelegateJob, text: str, capture: CaptureTarget) -> None:
        for line in text.splitlines():
            clean = line.strip()
            if clean.startswith(PROGRESS_PREFIX) and capture.record_activity:
                self._record_activity_locked(job, "progress", clean[len(PROGRESS_PREFIX) :].strip())
        filtered = strip_progress_lines(text)
        capture.assistant_path.write_text(filtered, encoding="utf-8")
        if capture.assistant_path == job.compact_assistant_path:
            job.compact_assistant_chars = len(filtered)
        else:
            job.assistant_chars = len(filtered)

    def _record_activity_locked(self, job: DelegateJob, kind: str, summary: str) -> None:
        clean = " ".join(summary.split())
        if not clean:
            return
        entry = {
            "at": time.time(),
            "kind": kind,
            "summary": short_text(clean, 500),
        }
        job.activity.append(entry)
        if len(job.activity) > MAX_ACTIVITY:
            del job.activity[: len(job.activity) - MAX_ACTIVITY]
        job.last_activity_at = entry["at"]
        self._write_activity_locked(job)

    def _write_activity_locked(self, job: DelegateJob) -> None:
        job.activity_path.write_text(json.dumps(job.activity, indent=2, sort_keys=True), encoding="utf-8")

    def _result_text(self, job: DelegateJob, cwd: Path) -> dict[str, Any]:
        text = read_text_if_exists(job.assistant_path)
        target = target_result_chars()
        if len(text) <= target:
            return {"result_state": "complete", "text": text}
        compact_text = self._compact_text(job, cwd)
        if compact_text is not None and len(compact_text) <= target:
            return {"result_state": "complete", "text": compact_text}
        return {"result_state": "result_oversized", "text": None}

    def _compact_text(self, job: DelegateJob, cwd: Path) -> str | None:
        command = self._compaction_command(job, job.assistant_path)
        capture = CaptureTarget(
            events_path=job.compact_events_path,
            assistant_path=job.compact_assistant_path,
            stderr_path=job.compact_stderr_path,
            stdout_bytes_field="compact_stdout_bytes",
            stderr_bytes_field="compact_stderr_bytes",
            json_events_field="compact_json_events",
            record_activity=False,
            record_errors=False,
        )
        try:
            result = self._run_process(job, command, cwd, capture, timeout=min(COMPACTION_TIMEOUT, max(job.timeout, 1)))
        except Exception as exc:  # noqa: BLE001 - failed compaction becomes result_oversized.
            with self._lock:
                self._record_activity_locked(job, "compaction_error", str(exc))
            return None
        if result.returncode != 0:
            with self._lock:
                self._record_activity_locked(job, "compaction_error", f"backend exited with return code {result.returncode}")
            return None
        return read_text_if_exists(job.compact_assistant_path)

    def _main_capture(self, job: DelegateJob) -> CaptureTarget:
        return CaptureTarget(
            events_path=job.events_path,
            assistant_path=job.assistant_path,
            stderr_path=job.stderr_path,
            stdout_bytes_field="stdout_bytes",
            stderr_bytes_field="stderr_bytes",
            json_events_field="json_events",
        )

    def _snapshot_locked(self, job: DelegateJob, *, include_details: bool = True) -> dict[str, Any]:
        now = job.completed_at or time.time()
        ready = job.state not in {"running", "cancelling"}
        data: dict[str, Any] = {
            "job_id": job.job_id,
            "mode": job.mode,
            "state": job.state,
            "ready": ready,
            "cwd": job.cwd,
            "model": job.model,
            "thinking": job.thinking,
            "timeout": job.timeout,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "elapsed_seconds": round(now - job.started_at, 3),
            "cancel_requested": job.cancel_requested,
            "artifacts": artifact_paths(job) if include_details or not ready else minimal_artifact_paths(job),
            "counters": counters(job) if include_details or not ready else minimal_counters(job),
        }
        if include_details or not ready:
            data["session"] = session_info(job)
            data["activity_tail"] = job.activity[-ACTIVITY_TAIL:]
            data["running_actions"] = list(job.running_tools.values())
        if job.error:
            data["error"] = job.error
        if job.backend_returncode is not None:
            data["backend_returncode"] = job.backend_returncode
        if include_details:
            data["prompt_preview"] = job.prompt_preview
            data["last_output_at"] = job.last_output_at
            data["last_activity_at"] = job.last_activity_at
            if job.backend_message_error:
                data["backend_message_error"] = job.backend_message_error
            if job.backend_terminal_error:
                data["backend_terminal_error"] = job.backend_terminal_error
        return data

    def _get_job_locked(self, job_id: str) -> DelegateJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise LocalAgentDelegateError(f"unknown job_id: {job_id}")
        return job

    def _prune(self) -> None:
        cutoff = time.time() - artifact_ttl_seconds()
        with self._lock:
            old_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.completed_at is not None and job.completed_at < cutoff
            ]
            for job_id in old_ids:
                self._jobs.pop(job_id, None)
        self._prune_artifacts()

    def _prune_artifacts(self) -> None:
        root = artifact_root()
        if not root.exists():
            return
        cutoff = time.time() - artifact_ttl_seconds()
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    remove_tree(child)
            except OSError:
                continue


def create_artifact_dir(job_id: str) -> Path:
    root = artifact_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    path = root / job_id
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


def initialize_artifacts(job: DelegateJob) -> None:
    for path in [
        job.events_path,
        job.assistant_path,
        job.stderr_path,
        job.activity_path,
    ]:
        path.write_text("[]" if path == job.activity_path else "", encoding="utf-8")


def artifact_paths(job: DelegateJob) -> dict[str, str | None]:
    data: dict[str, str | None] = {
        "dir": str(job.artifact_dir),
        "events_jsonl": str(job.events_path),
        "assistant_txt": str(job.assistant_path),
        "stderr_txt": str(job.stderr_path),
        "activity_json": str(job.activity_path),
        "diff_patch": str(job.diff_path) if job.diff_path.exists() else None,
        "compact_events_jsonl": str(job.compact_events_path) if job.compact_events_path.exists() else None,
        "compact_assistant_txt": str(job.compact_assistant_path) if job.compact_assistant_path.exists() else None,
        "compact_stderr_txt": str(job.compact_stderr_path) if job.compact_stderr_path.exists() else None,
    }
    return data


def minimal_artifact_paths(job: DelegateJob) -> dict[str, str]:
    data = {
        "dir": str(job.artifact_dir),
        "assistant_txt": str(job.assistant_path),
    }
    if job.compact_assistant_path.exists():
        data["compact_assistant_txt"] = str(job.compact_assistant_path)
    if job.diff_path.exists():
        data["diff_patch"] = str(job.diff_path)
    return data


def session_info(job: DelegateJob) -> dict[str, str | None]:
    return {
        "dir": str(job.session_dir),
        "id": job.session_id,
        "file": job.session_file,
    }


def counters(job: DelegateJob) -> dict[str, int]:
    total_stdout_bytes = job.stdout_bytes + job.compact_stdout_bytes
    total_stderr_bytes = job.stderr_bytes + job.compact_stderr_bytes
    total_stream_bytes = total_stdout_bytes + total_stderr_bytes
    delivered_assistant_chars = job.compact_assistant_chars or job.assistant_chars
    estimated_primary_tokens_avoided = max(0, total_stream_bytes - delivered_assistant_chars) // APPROX_BYTES_PER_TOKEN
    return {
        "stdout_bytes": job.stdout_bytes,
        "stderr_bytes": job.stderr_bytes,
        "assistant_chars": job.assistant_chars,
        "json_events": job.json_events,
        "compact_stdout_bytes": job.compact_stdout_bytes,
        "compact_stderr_bytes": job.compact_stderr_bytes,
        "compact_assistant_chars": job.compact_assistant_chars,
        "compact_json_events": job.compact_json_events,
        "total_stdout_bytes": total_stdout_bytes,
        "total_stderr_bytes": total_stderr_bytes,
        "total_stream_bytes": total_stream_bytes,
        "delivered_assistant_chars": delivered_assistant_chars,
        "estimated_primary_tokens_avoided_approx": estimated_primary_tokens_avoided,
        "estimation_bytes_per_token": APPROX_BYTES_PER_TOKEN,
    }


def minimal_counters(job: DelegateJob) -> dict[str, int]:
    values = counters(job)
    return {key: values[key] for key in MINIMAL_COUNTER_KEYS}


def result_for_response(job: DelegateJob, *, include_details: bool) -> dict[str, Any]:
    if job.result is None:
        return {}
    if include_details:
        return job.result

    result = job.result
    result_type = result.get("type")
    if result_type == "patch":
        keys = ("type", "result_state", "summary_text", "diff_artifact", "applied_to_current_tree")
    else:
        keys = ("type", "result_state", "text")
    data = {key: result[key] for key in keys if key in result}
    if result.get("result_state") == "result_oversized" and "target_chars" in result:
        data["target_chars"] = result["target_chars"]
    if "recovered_error" in result:
        data["recovered_error"] = result["recovered_error"]
    return data


def delegate_scout_skill_path() -> Path:
    path = Path(__file__).resolve().parent / "backend_skills" / "delegate_scout" / "SKILL.md"
    if not path.exists():
        raise LocalAgentDelegateError(f"bundled delegate scout skill not found: {path}")
    return path


def model_args(model: str | None) -> list[str]:
    return ["--model", model] if model else []


def terminate_process(proc: subprocess.Popen[str], *, kill: bool = False) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001 - fall back to direct process termination.
        if kill:
            proc.kill()
        else:
            proc.terminate()
    if kill:
        return
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        terminate_process(proc, kill=True)


def command_summary(command: list[str]) -> str:
    parts = []
    for arg in command:
        clean = arg.replace("\n", " ")
        parts.append(short_text(clean, 120))
    return " ".join(parts)


def strip_progress_lines(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith(PROGRESS_PREFIX):
            continue
        lines.append(line)
    return "".join(lines)


def assistant_text_from_message(message: Any) -> str | None:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"text", "output_text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def message_error(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    if message.get("stopReason") != "error":
        return None
    for key in ("errorMessage", "error", "message"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "message stopped with an error"


def messages_error(messages: Any) -> str | None:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        error = message_error(message)
        if error:
            return error
    return None


def summarize_tool_action(tool_name: str, args: Any) -> str:
    clean_name = tool_name.strip() or "tool"
    if not isinstance(args, dict):
        return clean_name
    if clean_name == "bash":
        command = string_value(args, "command") or string_value(args, "cmd") or string_value(args, "script")
        if command:
            return f"running bash: {one_line(command)}"
    target = first_string_value(
        args,
        ["path", "file", "file_path", "filePath", "target", "glob", "pattern", "query"],
    )
    if target:
        verb = {
            "read": "reading",
            "grep": "searching",
            "find": "finding",
            "ls": "listing",
            "edit": "editing",
            "write": "writing",
        }.get(clean_name, clean_name)
        return f"{verb} {one_line(target)}"
    return clean_name


def first_string_value(args: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = string_value(args, key)
        if value:
            return value
    return None


def string_value(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def json_string_from_line(line: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', line)
    if not match:
        return None
    try:
        value = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) and value.strip() else None


def json_bool_from_line(line: str, key: str) -> bool:
    return re.search(rf'"{re.escape(key)}"\s*:\s*true\b', line) is not None


def one_line(text: str) -> str:
    return " ".join(text.split())


def backend_returncode(result: dict[str, Any]) -> int:
    raw = result.get("backend_returncode", 0)
    if isinstance(raw, int):
        return raw
    return 0


def result_has_complete_text(result: dict[str, Any]) -> bool:
    if result.get("result_state") != "complete":
        return False
    for key in ("text", "summary_text"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        return
    if path.is_dir():
        for child in path.iterdir():
            remove_tree(child)
        path.rmdir()
    else:
        path.unlink()
