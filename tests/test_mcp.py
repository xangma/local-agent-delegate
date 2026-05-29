from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import local_agent_delegate.jobs as jobs_module
from local_agent_delegate.mcp import handle_request, handle_tool, tool_content


def write_fake_pi(directory: str, body: str) -> Path:
    path = Path(directory) / "fake-pi"
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def wait_for_job(job_id: str, desired: set[str] | None = None) -> dict[str, object]:
    desired = desired or {"succeeded", "failed", "cancelled"}
    status: dict[str, object] = {}
    for _ in range(80):
        status = handle_tool("local_agent_delegate_job_status", {"job_id": job_id})
        if status["state"] in desired:
            return status
        time.sleep(0.05)
    return status


class Env:
    def __init__(self, **values: str | None) -> None:
        self.values = values
        self.old: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            self.old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *_: object) -> None:
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class McpTests(unittest.TestCase):
    def test_tools_list_contains_expected_tools(self) -> None:
        result = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in result["tools"]}
        self.assertEqual(
            names,
            {
                "local_agent_delegate_policy",
                "local_agent_delegate_status",
                "local_agent_delegate_run_start",
                "local_agent_delegate_patch_start",
                "local_agent_delegate_job_status",
                "local_agent_delegate_job_result",
                "local_agent_delegate_job_wait",
                "local_agent_delegate_job_cancel",
                "local_agent_delegate_jobs",
            },
        )

    def test_resource_templates_list_is_supported(self) -> None:
        result = handle_request({"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"})
        self.assertEqual(result, {"resourceTemplates": []})

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(Exception):
            handle_tool("missing", {})

    def test_removed_sync_tools_fail_fast(self) -> None:
        with self.assertRaisesRegex(Exception, "local_agent_delegate_run has been removed"):
            handle_tool("local_agent_delegate_run", {"prompt": "wait", "cwd": "."})
        with self.assertRaisesRegex(Exception, "local_agent_delegate_patch has been removed"):
            handle_tool("local_agent_delegate_patch", {"prompt": "wait", "cwd": "."})

    def test_tool_content_returns_metadata_not_truncation_for_oversize(self) -> None:
        content = tool_content({"payload": "x" * 200_000})["content"][0]["text"]
        self.assertIn("tool_response_oversized", content)
        self.assertNotIn("[truncated", content)
        self.assertNotIn("x" * 1000, content)

    def test_policy_reports_token_efficient_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with Env(
                LOCAL_AGENT_DELEGATE_GOAL="save-on-tokens",
                LOCAL_AGENT_DELEGATE_TARGET_RESULT_CHARS="77",
                LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts"),
            ):
                policy = handle_tool("local_agent_delegate_policy", {})
                self.assertEqual(policy["goal"], "save-on-tokens")
                self.assertEqual(policy["target_result_chars"], 77)
                self.assertEqual(policy["result_profile"], "artifact-backed-compact")
                self.assertIn("session", policy["session_behavior"])
                self.assertIn("bundled delegate scout", policy["backend_resource_mode"])
                self.assertIn("wait", policy["workflow_guidance"])
                self.assertIn("backend was insufficient", policy["verification_budget"])

    def test_async_run_returns_compact_result_and_artifacts(self) -> None:
        body = """cat <<'JSON'
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Final answer\\n"}]}}
JSON
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            artifact_root = Path(tmp) / "artifacts"
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(artifact_root), LOCAL_AGENT_DELEGATE_MODEL=None):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "say hi", "cwd": tmp, "timeout": 5})
                self.assertEqual(started["mode"], "read_only")
                self.assertEqual(started["state"], "running")
                job_id = str(started["job_id"])
                status = wait_for_job(job_id)
                self.assertEqual(status["state"], "succeeded")
                self.assertIn("estimated_primary_tokens_avoided_approx", status["counters"])

                result = handle_tool("local_agent_delegate_job_result", {"job_id": job_id})
                self.assertTrue(result["ready"])
                self.assertEqual(result["result"]["type"], "read_only")
                self.assertEqual(result["result"]["result_state"], "complete")
                self.assertEqual(result["result"]["text"], "Final answer\n")
                self.assertNotIn("artifacts", result["result"])
                self.assertNotIn("activity_tail", result)
                self.assertNotIn("running_actions", result)
                self.assertNotIn("session", result)
                self.assertNotIn("stdout", result["result"])
                self.assertNotIn("stderr", result["result"])
                self.assertNotIn("stdout_tail", result)
                self.assertNotIn("assistant_tail", result)
                counters = result["counters"]
                self.assertEqual(set(counters), set(jobs_module.MINIMAL_COUNTER_KEYS))
                self.assertEqual(counters["delivered_assistant_chars"], len("Final answer\n"))
                self.assertGreater(counters["total_stream_bytes"], 0)
                self.assertGreaterEqual(counters["estimated_primary_tokens_avoided_approx"], 0)
                self.assertEqual(counters["estimation_bytes_per_token"], 4)

                artifacts = result["artifacts"]
                self.assertEqual(set(artifacts), {"dir", "assistant_txt"})
                self.assertEqual(Path(artifacts["assistant_txt"]).read_text(encoding="utf-8"), "Final answer\n")
                self.assertTrue(str(artifacts["dir"]).startswith(str(artifact_root.resolve())))

                details = handle_tool("local_agent_delegate_job_result", {"job_id": job_id, "include_details": True})
                self.assertIn("activity_tail", details)
                self.assertIn("running_actions", details)
                self.assertIn("session", details)
                self.assertIn("events_jsonl", details["artifacts"])
                self.assertIn("artifacts", details["result"])
                self.assertTrue(Path(details["artifacts"]["events_jsonl"]).exists())

    def test_async_run_command_uses_sessions_resources_skill_and_per_job_thinking(self) -> None:
        body = """printf '%s\\n' "$@" > args.txt
cat <<'JSON'
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"ok\\n"}]}}
JSON
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts"), LOCAL_AGENT_DELEGATE_MODEL=None):
                started = handle_tool(
                    "local_agent_delegate_run_start",
                    {"prompt": "say hi", "cwd": tmp, "timeout": 5, "thinking": "low"},
                )
                self.assertEqual(started["thinking"], "low")
                status = wait_for_job(str(started["job_id"]))
                self.assertEqual(status["state"], "succeeded")

                args = (Path(tmp) / "args.txt").read_text(encoding="utf-8").splitlines()
                self.assertIn("--mode", args)
                self.assertEqual(args[args.index("--mode") + 1], "json")
                self.assertIn("--session-dir", args)
                self.assertNotIn("--no-session", args)
                self.assertIn("--no-extensions", args)
                self.assertIn("--no-skills", args)
                self.assertIn("--skill", args)
                self.assertIn("delegate_scout", args[args.index("--skill") + 1])
                self.assertIn("--no-prompt-templates", args)
                self.assertIn("--no-themes", args)
                self.assertIn("--tools", args)
                self.assertEqual(args[args.index("--tools") + 1], "read,grep,find,ls")
                self.assertNotIn("--model", args)
                self.assertIn("--thinking", args)
                self.assertEqual(args[args.index("--thinking") + 1], "low")

    def test_async_run_command_uses_configured_model_when_set(self) -> None:
        body = """printf '%s\\n' "$@" > args.txt
cat <<'JSON'
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"ok\\n"}]}}
JSON
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(
                LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi),
                LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts"),
                LOCAL_AGENT_DELEGATE_MODEL="provider/model",
            ):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "say hi", "cwd": tmp, "timeout": 5})
                status = wait_for_job(str(started["job_id"]))
                self.assertEqual(status["state"], "succeeded")

                args = (Path(tmp) / "args.txt").read_text(encoding="utf-8").splitlines()
                self.assertIn("--model", args)
                self.assertEqual(args[args.index("--model") + 1], "provider/model")

    def test_default_thinking_omits_thinking_arg(self) -> None:
        body = """printf '%s\\n' "$@" > args.txt
printf 'plain fallback\\n'
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(
                LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi),
                LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts"),
                LOCAL_AGENT_DELEGATE_THINKING="default",
            ):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "say hi", "cwd": tmp, "timeout": 5})
                status = wait_for_job(str(started["job_id"]))
                self.assertEqual(status["state"], "succeeded")
                args = (Path(tmp) / "args.txt").read_text(encoding="utf-8").splitlines()
                self.assertNotIn("--thinking", args)

    def test_huge_tool_result_stays_in_artifact_not_status(self) -> None:
        body = """python3 - <<'PY'
import json
print(json.dumps({"type":"tool_execution_start","toolCallId":"read-1","toolName":"read","args":{"path":"src/foo.py"}}))
print(json.dumps({"type":"tool_execution_end","toolCallId":"read-1","toolName":"read","result":"x"*200000,"isError":False}))
print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Done\\n"}]}}))
PY
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts")):
                original_loads = jobs_module.json.loads

                def guarded_loads(value: str) -> object:
                    if len(value) > jobs_module.JSON_PARSE_CHAR_LIMIT:
                        raise AssertionError("large event payload should not be fully parsed")
                    return original_loads(value)

                with mock.patch.object(jobs_module.json, "loads", side_effect=guarded_loads):
                    started = handle_tool("local_agent_delegate_run_start", {"prompt": "inspect", "cwd": tmp, "timeout": 5})
                    status = wait_for_job(str(started["job_id"]))
                    self.assertEqual(status["state"], "succeeded")
                    serialized_status = json.dumps(status)
                    self.assertLess(len(serialized_status), 20_000)
                    self.assertNotIn("stdout_tail", status)
                    self.assertNotIn("x" * 1000, serialized_status)
                    self.assertGreater(Path(status["artifacts"]["events_jsonl"]).stat().st_size, 200_000)

    def test_rejected_job_start_does_not_leave_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp) / "artifacts"
            with Env(LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(artifact_root)):
                manager = jobs_module.JobManager()
                with mock.patch.object(jobs_module, "MAX_JOBS", 0):
                    with self.assertRaisesRegex(Exception, "too many local-agent-delegate jobs"):
                        manager.start_read_only(prompt="inspect", cwd=tmp, timeout=5)
                self.assertFalse(artifact_root.exists() and any(artifact_root.iterdir()))

    def test_remove_tree_unlinks_symlink_without_following_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifact"
            outside = Path(tmp) / "outside"
            outside.mkdir()
            marker = outside / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            root.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)

            jobs_module.remove_tree(root)

            self.assertFalse(root.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_oversized_result_compacts_without_truncation(self) -> None:
        body = """count_file=count.txt
count=0
if [ -f "$count_file" ]; then count=$(cat "$count_file"); fi
count=$((count + 1))
printf '%s' "$count" > "$count_file"
if [ "$count" -eq 1 ]; then
  python3 - <<'PY'
import json
print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"A"*200}]}}))
PY
else
  cat <<'JSON'
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Compact result\\n"}]}}
JSON
fi
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(
                LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi),
                LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts"),
                LOCAL_AGENT_DELEGATE_TARGET_RESULT_CHARS="50",
            ):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "inspect", "cwd": tmp, "timeout": 5})
                result = handle_tool("local_agent_delegate_job_wait", {"job_id": str(started["job_id"]), "wait_timeout": 5})
                self.assertTrue(result["ready"])
                self.assertEqual(result["result"]["result_state"], "complete")
                self.assertEqual(result["result"]["text"], "Compact result\n")
                self.assertNotIn("[truncated", json.dumps(result))
                self.assertTrue(Path(result["artifacts"]["compact_assistant_txt"]).exists())

    def test_uncompactable_result_returns_metadata_only(self) -> None:
        body = """python3 - <<'PY'
import json
print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"A"*200}]}}))
PY
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(
                LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi),
                LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts"),
                LOCAL_AGENT_DELEGATE_TARGET_RESULT_CHARS="10",
            ):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "inspect", "cwd": tmp, "timeout": 5})
                result = handle_tool("local_agent_delegate_job_wait", {"job_id": str(started["job_id"]), "wait_timeout": 5})
                self.assertTrue(result["ready"])
                self.assertEqual(result["result"]["result_state"], "result_oversized")
                self.assertIsNone(result["result"]["text"])
                self.assertNotIn("[truncated", json.dumps(result))
                self.assertGreater(Path(result["artifacts"]["assistant_txt"]).stat().st_size, 10)

    def test_job_wait_times_out_cleanly_and_cancel_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, "sleep 30\n")
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts")):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "wait", "cwd": tmp, "timeout": 30})
                wait = handle_tool("local_agent_delegate_job_wait", {"job_id": str(started["job_id"]), "wait_timeout": 1})
                self.assertFalse(wait["ready"])
                self.assertTrue(wait["timed_out"])
                self.assertIn("activity_tail", wait)
                self.assertIn("running_actions", wait)
                cancelled = handle_tool("local_agent_delegate_job_cancel", {"job_id": str(started["job_id"])})
                self.assertIn(cancelled["state"], {"cancelling", "cancelled"})
                status = wait_for_job(str(started["job_id"]), {"cancelled"})
                self.assertEqual(status["state"], "cancelled")

    def test_job_wait_rejects_oversized_timeout(self) -> None:
        with self.assertRaisesRegex(Exception, "wait_timeout must be between 1 and 90"):
            handle_tool("local_agent_delegate_job_wait", {"job_id": "missing", "wait_timeout": 120000})

    def test_json_retry_failure_fails_job_with_compact_error(self) -> None:
        body = """cat <<'JSON'
{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"Connection error."}}
{"type":"agent_end","messages":[{"role":"assistant","content":[],"stopReason":"error","errorMessage":"Connection error."}],"willRetry":false}
{"type":"auto_retry_end","success":false,"attempt":3,"finalError":"Connection error."}
JSON
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts")):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "inspect", "cwd": tmp, "timeout": 5})
                status = wait_for_job(str(started["job_id"]), {"failed"})
                self.assertEqual(status["state"], "failed")
                result = handle_tool("local_agent_delegate_job_result", {"job_id": str(started["job_id"])})
                self.assertTrue(result["ready"])
                self.assertEqual(result["error"], "backend reported error: Connection error.")
                self.assertNotIn("stdout_tail", result)

    def test_recovered_backend_error_with_complete_result_succeeds_with_warning(self) -> None:
        body = """cat <<'JSON'
{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"400 request (105387 tokens) exceeds the available context size (98304 tokens), try increasing it"}}
{"type":"agent_end","messages":[{"role":"assistant","content":[],"stopReason":"error","errorMessage":"400 request (105387 tokens) exceeds the available context size (98304 tokens), try increasing it"}],"willRetry":false}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Compact recovered result\\n"}]}}
JSON
"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, body)
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts")):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "inspect", "cwd": tmp, "timeout": 5})
                result = handle_tool(
                    "local_agent_delegate_job_wait",
                    {"job_id": str(started["job_id"]), "wait_timeout": 5, "include_details": True},
                )
                self.assertEqual(result["state"], "succeeded")
                self.assertNotIn("error", result)
                self.assertEqual(result["backend_terminal_error"], "400 request (105387 tokens) exceeds the available context size (98304 tokens), try increasing it")
                self.assertEqual(result["result"]["text"], "Compact recovered result\n")
                self.assertEqual(result["result"]["recovered_error"], result["backend_terminal_error"])

    def test_nonzero_exit_fails_with_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_pi = write_fake_pi(tmp, "printf 'bad\\n' >&2\nexit 7\n")
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts")):
                started = handle_tool("local_agent_delegate_run_start", {"prompt": "fail", "cwd": tmp, "timeout": 5})
                result = handle_tool("local_agent_delegate_job_wait", {"job_id": str(started["job_id"]), "wait_timeout": 5})
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["backend_returncode"], 7)
                self.assertEqual(result["error"], "backend exited with return code 7")
                self.assertNotIn("stderr_txt", result["artifacts"])

                details = handle_tool(
                    "local_agent_delegate_job_result",
                    {"job_id": str(started["job_id"]), "include_details": True},
                )
                self.assertEqual(Path(details["artifacts"]["stderr_txt"]).read_text(encoding="utf-8"), "bad\n")

    def test_patch_job_writes_diff_artifact_without_inline_diff(self) -> None:
        body = """printf 'changed\\n' > file.txt
cat <<'JSON'
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Changed file.txt\\n"}]}}
JSON
"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "file.txt").write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

            fake_pi = write_fake_pi(tmp, body)
            with Env(LOCAL_AGENT_DELEGATE_PI_BIN=str(fake_pi), LOCAL_AGENT_DELEGATE_ARTIFACT_ROOT=str(Path(tmp) / "artifacts")):
                started = handle_tool("local_agent_delegate_patch_start", {"prompt": "change file", "cwd": str(repo), "timeout": 5})
                result = handle_tool("local_agent_delegate_job_wait", {"job_id": str(started["job_id"]), "wait_timeout": 5})
                self.assertEqual(result["state"], "succeeded")
                patch = result["result"]
                self.assertEqual(patch["type"], "patch")
                self.assertEqual(patch["summary_text"], "Changed file.txt\n")
                self.assertNotIn("diff", patch)
                diff_path = Path(patch["diff_artifact"])
                self.assertIn("-original", diff_path.read_text(encoding="utf-8"))
                self.assertIn("+changed", diff_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
