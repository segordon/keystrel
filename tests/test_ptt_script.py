import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PTT_SCRIPT = REPO_ROOT / "bin" / "keystrel-ptt"


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_forwarding_mkdir(path):
    _write_executable(
        path,
        """#!/bin/bash
set -euo pipefail
/usr/bin/mkdir "$@"
""",
    )


class PTTScriptBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.fake_bin = self.temp_dir / "fake-bin"
        self.fake_bin.mkdir(parents=True, exist_ok=True)

        self.xdotool_log = self.temp_dir / "xdotool.log"
        self.client_log = self.temp_dir / "client.log"
        self.client_env_log = self.temp_dir / "client-env.log"

        _write_executable(
            self.fake_bin / "xdotool",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${XDOTOOL_LOG:?}"
""",
        )

        _write_executable(
            self.fake_bin / "keystrel-client",
            """#!/usr/bin/env bash
set -euo pipefail
printf 'call\n' >>"${KEYSTREL_CLIENT_CALL_LOG:?}"
if [[ -n "${KEYSTREL_CLIENT_ENV_LOG:-}" ]]; then
  printf 'mute_start_delay=%s input_device=%s sample_rate=%s mute_settle=%s start_chime=%s cancel_file=%s\n' \
    "${KEYSTREL_MUTE_START_DELAY_MS:-}" \
    "${KEYSTREL_INPUT_DEVICE:-}" \
    "${KEYSTREL_SAMPLE_RATE:-}" \
    "${KEYSTREL_MUTE_SETTLE_MS:-}" \
    "${KEYSTREL_START_CHIME:-}" \
    "${KEYSTREL_CANCEL_FILE:-}" >>"${KEYSTREL_CLIENT_ENV_LOG}"
fi
if [[ "${KEYSTREL_CLIENT_CANCEL_WATCH_MS:-}" =~ ^[0-9]+$ ]]; then
  deadline_ms=$(( $(date +%s%3N) + KEYSTREL_CLIENT_CANCEL_WATCH_MS ))
  while (( $(date +%s%3N) < deadline_ms )); do
    if [[ -n "${KEYSTREL_CANCEL_FILE:-}" && -f "${KEYSTREL_CANCEL_FILE}" ]]; then
      exit 0
    fi
    sleep 0.01
  done
fi
if [[ -n "${KEYSTREL_CANCEL_FILE:-}" && -f "${KEYSTREL_CANCEL_FILE}" ]]; then
  exit 0
fi
if [[ -n "${KEYSTREL_CLIENT_SLEEP_MS:-}" ]]; then
  s=$((KEYSTREL_CLIENT_SLEEP_MS / 1000))
  ms=$((KEYSTREL_CLIENT_SLEEP_MS % 1000))
  sleep "${s}.$(printf '%03d' "$ms")"
fi
printf '%s' "${KEYSTREL_CLIENT_TEXT:-hello world}"
""",
        )

        self.base_env = os.environ.copy()
        self.base_env.update(
            {
                "XDG_SESSION_TYPE": "x11",
                "XDG_RUNTIME_DIR": str(self.temp_dir / "runtime"),
                "KEYSTREL_CLIENT_BIN": str(self.fake_bin / "keystrel-client"),
                "KEYSTREL_PTT_CHIME_ENABLED": "0",
                "KEYSTREL_PTT_CHIME_TARGET": "dummy-target",
                "KEYSTREL_PTT_SEND_ENTER": "0",
                "XDOTOOL_LOG": str(self.xdotool_log),
                "KEYSTREL_CLIENT_CALL_LOG": str(self.client_log),
                "PATH": f"{self.fake_bin}:{self.base_env.get('PATH', '')}",
            }
        )

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def _run_ptt(self, env_overrides=None):
        env = dict(self.base_env)
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            ["/bin/bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )

    def _cancel_flag_path(self):
        return Path(self.base_env["XDG_RUNTIME_DIR"]) / "keystrel-ptt.cancel"

    def test_debounce_suppresses_second_invocation(self):
        first = self._run_ptt({"KEYSTREL_PTT_DEBOUNCE_MS": "60000"})
        second = self._run_ptt({"KEYSTREL_PTT_DEBOUNCE_MS": "60000"})

        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)

        xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(xdotool_lines), 1)
        self.assertEqual(len(client_lines), 1)

    def test_forwards_mute_and_input_env_to_client(self):
        result = self._run_ptt(
            {
                "KEYSTREL_PTT_MUTE_START_DELAY_MS": "240",
                "KEYSTREL_INPUT_DEVICE": "UM10",
                "KEYSTREL_SAMPLE_RATE": "48000",
                "KEYSTREL_MUTE_SETTLE_MS": "350",
                "KEYSTREL_CLIENT_ENV_LOG": str(self.client_env_log),
                "KEYSTREL_CLIENT_TEXT": "captured",
            }
        )

        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout} stderr={result.stderr}")
        env_line = self.client_env_log.read_text(encoding="utf-8").strip()
        self.assertIn("mute_start_delay=240", env_line)
        self.assertIn("input_device=UM10", env_line)
        self.assertIn("sample_rate=48000", env_line)
        self.assertIn("mute_settle=350", env_line)
        self.assertIn("start_chime=0", env_line)
        self.assertIn("cancel_file=", env_line)

    def test_invalid_ptt_mute_delay_falls_back_to_zero(self):
        result = self._run_ptt(
            {
                "KEYSTREL_PTT_MUTE_START_DELAY_MS": "not-a-number",
                "KEYSTREL_CLIENT_ENV_LOG": str(self.client_env_log),
                "KEYSTREL_CLIENT_TEXT": "captured",
            }
        )

        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout} stderr={result.stderr}")
        env_line = self.client_env_log.read_text(encoding="utf-8").strip()
        self.assertIn("mute_start_delay=0", env_line)

    def test_cancel_flag_removed_after_normal_run(self):
        result = self._run_ptt({"KEYSTREL_CLIENT_TEXT": "hello"})

        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout} stderr={result.stderr}")
        self.assertFalse(self._cancel_flag_path().exists())

    def test_lock_prevents_overlapping_runs(self):
        env = dict(self.base_env)
        env.update({"KEYSTREL_PTT_DEBOUNCE_MS": "0", "KEYSTREL_CLIENT_SLEEP_MS": "400"})

        first = subprocess.Popen(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.05)
        second = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=5.0)

        self.assertEqual(first.returncode, 0, msg=f"stdout={first_stdout} stderr={first_stderr}")
        self.assertEqual(second.returncode, 0, msg=f"stdout={second.stdout} stderr={second.stderr}")

        xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(xdotool_lines), 1)
        self.assertEqual(len(client_lines), 1)

    def test_second_press_requests_cancel_and_skips_typing(self):
        env = dict(self.base_env)
        env.update(
            {
                "KEYSTREL_PTT_DEBOUNCE_MS": "0",
                "KEYSTREL_PTT_CANCEL_DEBOUNCE_MS": "80",
                "KEYSTREL_CLIENT_CANCEL_WATCH_MS": "900",
                "KEYSTREL_CLIENT_TEXT": "should not type",
            }
        )

        first = subprocess.Popen(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.12)
        second = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=5.0)

        self.assertEqual(first.returncode, 0, msg=f"stdout={first_stdout} stderr={first_stderr}")
        self.assertEqual(second.returncode, 0, msg=f"stdout={second.stdout} stderr={second.stderr}")

        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(client_lines), 1)
        if self.xdotool_log.exists():
            xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(xdotool_lines), 0)
        self.assertFalse(self._cancel_flag_path().exists())

    def test_repeat_like_overlap_is_ignored_then_manual_press_cancels(self):
        env = dict(self.base_env)
        env.update(
            {
                "KEYSTREL_PTT_DEBOUNCE_MS": "0",
                "KEYSTREL_PTT_CANCEL_DEBOUNCE_MS": "80",
                "KEYSTREL_PTT_REPEAT_DELAY_MS": "120",
                "KEYSTREL_PTT_REPEAT_INTERVAL_MS": "30",
                "KEYSTREL_CLIENT_CANCEL_WATCH_MS": "1200",
                "KEYSTREL_CLIENT_TEXT": "should not type",
            }
        )

        first = subprocess.Popen(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.14)
        repeat_like = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        time.sleep(0.20)
        manual_press = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=5.0)

        self.assertEqual(first.returncode, 0, msg=f"stdout={first_stdout} stderr={first_stderr}")
        self.assertEqual(
            repeat_like.returncode,
            0,
            msg=f"stdout={repeat_like.stdout} stderr={repeat_like.stderr}",
        )
        self.assertEqual(
            manual_press.returncode,
            0,
            msg=f"stdout={manual_press.stdout} stderr={manual_press.stderr}",
        )

        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(client_lines), 1)
        if self.xdotool_log.exists():
            xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(xdotool_lines), 0)
        self.assertFalse(self._cancel_flag_path().exists())

    def test_second_press_ignored_when_cancel_disabled(self):
        env = dict(self.base_env)
        env.update(
            {
                "KEYSTREL_PTT_DEBOUNCE_MS": "0",
                "KEYSTREL_PTT_DOUBLE_PRESS_CANCEL": "0",
                "KEYSTREL_CLIENT_SLEEP_MS": "400",
                "KEYSTREL_CLIENT_TEXT": "typed",
            }
        )

        first = subprocess.Popen(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.10)
        second = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=5.0)

        self.assertEqual(first.returncode, 0, msg=f"stdout={first_stdout} stderr={first_stderr}")
        self.assertEqual(second.returncode, 0, msg=f"stdout={second.stdout} stderr={second.stderr}")

        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(client_lines), 1)
        self.assertEqual(len(xdotool_lines), 1)

    def test_invalid_cancel_toggle_value_falls_back_to_enabled(self):
        env = dict(self.base_env)
        env.update(
            {
                "KEYSTREL_PTT_DEBOUNCE_MS": "0",
                "KEYSTREL_PTT_DOUBLE_PRESS_CANCEL": "bad-value",
                "KEYSTREL_PTT_CANCEL_DEBOUNCE_MS": "80",
                "KEYSTREL_CLIENT_CANCEL_WATCH_MS": "900",
                "KEYSTREL_CLIENT_TEXT": "should not type",
            }
        )

        first = subprocess.Popen(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.12)
        second = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=5.0)

        self.assertEqual(first.returncode, 0, msg=f"stdout={first_stdout} stderr={first_stderr}")
        self.assertEqual(second.returncode, 0, msg=f"stdout={second.stdout} stderr={second.stderr}")

        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(client_lines), 1)
        if self.xdotool_log.exists():
            xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(xdotool_lines), 0)

    def test_invalid_cancel_debounce_falls_back_to_default(self):
        env = dict(self.base_env)
        env.update(
            {
                "KEYSTREL_PTT_DEBOUNCE_MS": "0",
                "KEYSTREL_PTT_CANCEL_DEBOUNCE_MS": "not-a-number",
                "KEYSTREL_CLIENT_CANCEL_WATCH_MS": "250",
                "KEYSTREL_CLIENT_TEXT": "typed",
            }
        )

        first = subprocess.Popen(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.10)
        second = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=5.0)

        self.assertEqual(first.returncode, 0, msg=f"stdout={first_stdout} stderr={first_stderr}")
        self.assertEqual(second.returncode, 0, msg=f"stdout={second.stdout} stderr={second.stderr}")

        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(client_lines), 1)
        self.assertEqual(len(xdotool_lines), 1)

    def test_cancel_debounce_ignores_immediate_repeat(self):
        env = dict(self.base_env)
        env.update(
            {
                "KEYSTREL_PTT_DEBOUNCE_MS": "0",
                "KEYSTREL_PTT_CANCEL_DEBOUNCE_MS": "500",
                "KEYSTREL_CLIENT_CANCEL_WATCH_MS": "250",
                "KEYSTREL_CLIENT_TEXT": "typed",
            }
        )

        first = subprocess.Popen(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.05)
        second = subprocess.run(
            ["bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=5.0)

        self.assertEqual(first.returncode, 0, msg=f"stdout={first_stdout} stderr={first_stderr}")
        self.assertEqual(second.returncode, 0, msg=f"stdout={second.stdout} stderr={second.stderr}")

        client_lines = self.client_log.read_text(encoding="utf-8").splitlines()
        xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(client_lines), 1)
        self.assertEqual(len(xdotool_lines), 1)

    def test_requires_x11_session(self):
        result = self._run_ptt({"XDG_SESSION_TYPE": "wayland"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires an X11 session", result.stderr)

    def test_reports_missing_client_binary(self):
        missing_client = self.temp_dir / "missing-client"
        result = self._run_ptt({"KEYSTREL_CLIENT_BIN": str(missing_client)})
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing executable keystrel-client", result.stderr)

    def test_reports_missing_xdotool(self):
        (self.fake_bin / "xdotool").unlink(missing_ok=True)
        _write_forwarding_mkdir(self.fake_bin / "mkdir")
        _write_executable(
            self.fake_bin / "flock",
            """#!/bin/bash
set -euo pipefail
exit 0
""",
        )
        result = self._run_ptt({"PATH": str(self.fake_bin)})
        self.assertEqual(result.returncode, 4)
        self.assertIn("missing xdotool", result.stderr)

    def test_reports_missing_flock(self):
        env = dict(self.base_env)
        env.update(
            {
                "PATH": str(self.fake_bin),
                "XDG_SESSION_TYPE": "x11",
            }
        )
        _write_forwarding_mkdir(self.fake_bin / "mkdir")
        result = subprocess.run(
            ["/bin/bash", str(PTT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        self.assertEqual(result.returncode, 5)
        self.assertIn("missing flock", result.stderr)

    def test_send_enter_types_return_key(self):
        result = self._run_ptt({"KEYSTREL_PTT_SEND_ENTER": "1", "KEYSTREL_CLIENT_TEXT": "typed"})
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout} stderr={result.stderr}")

        xdotool_lines = self.xdotool_log.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(xdotool_lines), 2)
        self.assertTrue(any(line.startswith("type --clearmodifiers") for line in xdotool_lines))
        self.assertTrue(any(line.startswith("key --clearmodifiers Return") for line in xdotool_lines))


if __name__ == "__main__":
    unittest.main()
