from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from local_agent_delegate.policy import policy_summary
from local_agent_delegate.runner import LocalAgentDelegateError, default_model, patch_prompt, pi_executable, pi_subprocess_env, resolve_cwd, status, thinking_args


class RunnerTests(unittest.TestCase):
    def test_resolve_cwd_rejects_missing(self) -> None:
        with self.assertRaises(LocalAgentDelegateError):
            resolve_cwd("/path/that/does/not/exist")

    def test_resolve_cwd_accepts_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(resolve_cwd(tmp), Path(tmp).resolve())

    def test_patch_prompt_contains_safety_constraints(self) -> None:
        text = patch_prompt("fix the bug")
        self.assertIn("disposable git worktree", text)
        self.assertIn("Do not commit", text)
        self.assertIn("fix the bug", text)

    def test_policy_honors_env(self) -> None:
        old_value = os.environ.get("LOCAL_AGENT_DELEGATE_LEAN")
        os.environ["LOCAL_AGENT_DELEGATE_LEAN"] = "conservative"
        try:
            self.assertEqual(policy_summary()["level"], "conservative")
        finally:
            if old_value is None:
                os.environ.pop("LOCAL_AGENT_DELEGATE_LEAN", None)
            else:
                os.environ["LOCAL_AGENT_DELEGATE_LEAN"] = old_value

    def test_policy_redelegation_threshold_uses_lean_and_goal(self) -> None:
        old_level = os.environ.get("LOCAL_AGENT_DELEGATE_LEAN")
        old_goal = os.environ.get("LOCAL_AGENT_DELEGATE_GOAL")
        cases = [
            ("off", "balanced", "explicit user request only"),
            ("conservative", "balanced", "6+ file reads/searches"),
            ("balanced", "balanced", "3+ file reads/searches"),
            ("aggressive", "balanced", "2+ file reads/searches"),
            ("conservative", "save-on-tokens", "3+ file reads/searches"),
            ("balanced", "save-on-tokens", "2+ file reads/searches"),
        ]
        try:
            for level, goal, expected in cases:
                os.environ["LOCAL_AGENT_DELEGATE_LEAN"] = level
                os.environ["LOCAL_AGENT_DELEGATE_GOAL"] = goal
                self.assertIn(expected, policy_summary()["redelegation_threshold"])
        finally:
            if old_level is None:
                os.environ.pop("LOCAL_AGENT_DELEGATE_LEAN", None)
            else:
                os.environ["LOCAL_AGENT_DELEGATE_LEAN"] = old_level
            if old_goal is None:
                os.environ.pop("LOCAL_AGENT_DELEGATE_GOAL", None)
            else:
                os.environ["LOCAL_AGENT_DELEGATE_GOAL"] = old_goal

    def test_default_model_honors_env(self) -> None:
        old_value = os.environ.get("LOCAL_AGENT_DELEGATE_MODEL")
        try:
            os.environ.pop("LOCAL_AGENT_DELEGATE_MODEL", None)
            self.assertIsNone(default_model())
            os.environ["LOCAL_AGENT_DELEGATE_MODEL"] = ""
            self.assertIsNone(default_model())
            os.environ["LOCAL_AGENT_DELEGATE_MODEL"] = "provider/model"
            self.assertEqual(default_model(), "provider/model")
        finally:
            if old_value is None:
                os.environ.pop("LOCAL_AGENT_DELEGATE_MODEL", None)
            else:
                os.environ["LOCAL_AGENT_DELEGATE_MODEL"] = old_value

    def test_status_reports_missing_pi_without_raising(self) -> None:
        old_pi = os.environ.get("LOCAL_AGENT_DELEGATE_PI_BIN")
        try:
            os.environ.pop("LOCAL_AGENT_DELEGATE_PI_BIN", None)
            with mock.patch("local_agent_delegate.runner.shutil.which", return_value=None):
                result = status()
            self.assertFalse(result["available"])
            self.assertEqual(result["reason"], "backend_not_found")
            self.assertIn("LOCAL_AGENT_DELEGATE_PI_BIN", result["install_hint"])
        finally:
            if old_pi is None:
                os.environ.pop("LOCAL_AGENT_DELEGATE_PI_BIN", None)
            else:
                os.environ["LOCAL_AGENT_DELEGATE_PI_BIN"] = old_pi

    def test_thinking_args_honors_env(self) -> None:
        old_value = os.environ.get("LOCAL_AGENT_DELEGATE_THINKING")
        try:
            os.environ.pop("LOCAL_AGENT_DELEGATE_THINKING", None)
            self.assertEqual(thinking_args(), [])
            os.environ["LOCAL_AGENT_DELEGATE_THINKING"] = "high"
            self.assertEqual(thinking_args(), ["--thinking", "high"])
            os.environ["LOCAL_AGENT_DELEGATE_THINKING"] = "whatever"
            self.assertEqual(thinking_args(), [])
        finally:
            if old_value is None:
                os.environ.pop("LOCAL_AGENT_DELEGATE_THINKING", None)
            else:
                os.environ["LOCAL_AGENT_DELEGATE_THINKING"] = old_value

    def test_pi_subprocess_env_prefers_pi_binary_node(self) -> None:
        old_path = os.environ.get("PATH")
        old_node = os.environ.get("LOCAL_AGENT_DELEGATE_NODE_BIN")
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "pi"
            executable.touch()
            os.environ["PATH"] = "/usr/local/bin"
            os.environ.pop("LOCAL_AGENT_DELEGATE_NODE_BIN", None)
            try:
                env = pi_subprocess_env(str(executable))
                self.assertEqual(env["PATH"].split(os.pathsep)[0], tmp)
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path
                if old_node is None:
                    os.environ.pop("LOCAL_AGENT_DELEGATE_NODE_BIN", None)
                else:
                    os.environ["LOCAL_AGENT_DELEGATE_NODE_BIN"] = old_node

    def test_pi_subprocess_env_uses_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            dist_dir = root / "lib/node_modules/pi/dist"
            bin_dir.mkdir()
            dist_dir.mkdir(parents=True)
            target = dist_dir / "cli.js"
            target.touch()
            executable = bin_dir / "pi"
            executable.symlink_to(target)

            env = pi_subprocess_env(str(executable))

            self.assertEqual(env["PATH"].split(os.pathsep)[0], str(bin_dir))

    @unittest.skipUnless(os.environ.get("LOCAL_AGENT_DELEGATE_REAL_PI_SMOKE") == "1", "set LOCAL_AGENT_DELEGATE_REAL_PI_SMOKE=1 to check the real Pi CLI")
    def test_real_pi_cli_supports_required_flags(self) -> None:
        result = subprocess.run([pi_executable(), "--help"], text=True, capture_output=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0)
        help_text = result.stdout + result.stderr
        self.assertTrue(help_text)
        for flag in [
            "--mode",
            "--session-dir",
            "--skill",
            "--no-skills",
            "--no-extensions",
            "--no-prompt-templates",
            "--tools",
            "--thinking",
        ]:
            self.assertIn(flag, help_text)


if __name__ == "__main__":
    unittest.main()
