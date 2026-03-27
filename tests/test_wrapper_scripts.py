import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_WRAPPER = REPO_ROOT / "bin" / "keystrel-client"
DAEMON_WRAPPER = REPO_ROOT / "bin" / "keystrel-daemon"
UNMUTE_WRAPPER = REPO_ROOT / "bin" / "keystrel-unmute"


def _write_file(path, content):
    path.write_text(content, encoding="utf-8")


class WrapperScriptSymlinkTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.fake_venv = self.temp_dir / "fake-venv"
        (self.fake_venv / "bin").mkdir(parents=True, exist_ok=True)

        fake_python = self.fake_venv / "bin" / "python"
        _write_file(
            fake_python,
            f"#!/usr/bin/env bash\nexec \"{sys.executable}\" \"$@\"\n",
        )
        fake_python.chmod(0o755)

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def _run_wrapper_via_symlink(self, wrapper_path, script_env_name, role):
        payload = self.temp_dir / f"{role}_payload.py"
        _write_file(
            payload,
            (
                "import json\n"
                "import sys\n"
                f"print(json.dumps({{'role': '{role}', 'args': sys.argv[1:]}}))\n"
            ),
        )

        wrapper_link = self.temp_dir / f"{wrapper_path.name}-link"
        wrapper_link.symlink_to(wrapper_path)

        env = os.environ.copy()
        env.update(
            {
                "KEYSTREL_VENV_DIR": str(self.fake_venv),
                script_env_name: str(payload),
            }
        )

        return subprocess.run(
            ["bash", str(wrapper_link), "--via-symlink"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )

    def test_client_wrapper_symlink_uses_repo_defaults(self):
        result = self._run_wrapper_via_symlink(
            CLIENT_WRAPPER,
            "KEYSTREL_CLIENT_PY",
            "client",
        )

        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout} stderr={result.stderr}")
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["role"], "client")
        self.assertEqual(payload["args"], ["--via-symlink"])

    def test_daemon_wrapper_symlink_uses_repo_defaults(self):
        result = self._run_wrapper_via_symlink(
            DAEMON_WRAPPER,
            "KEYSTREL_DAEMON_PY",
            "daemon",
        )

        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout} stderr={result.stderr}")
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["role"], "daemon")
        self.assertEqual(payload["args"], ["--via-symlink"])

    def test_unmute_wrapper_symlink_forwards_recover_flag(self):
        fake_client = self.temp_dir / "fake-client"
        _write_file(
            fake_client,
            (
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "print(json.dumps({'args': sys.argv[1:]}))\n"
            ),
        )
        fake_client.chmod(0o755)

        wrapper_link = self.temp_dir / "keystrel-unmute-link"
        wrapper_link.symlink_to(UNMUTE_WRAPPER)

        env = os.environ.copy()
        env.update({"KEYSTREL_CLIENT_BIN": str(fake_client)})

        result = subprocess.run(
            ["bash", str(wrapper_link), "--via-symlink"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout} stderr={result.stderr}")
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["args"], ["--recover-output-mute", "--via-symlink"])

    def test_unmute_wrapper_reports_missing_client_binary(self):
        wrapper_link = self.temp_dir / "keystrel-unmute-link"
        wrapper_link.symlink_to(UNMUTE_WRAPPER)

        env = os.environ.copy()
        env.update({"KEYSTREL_CLIENT_BIN": str(self.temp_dir / "does-not-exist")})

        result = subprocess.run(
            ["bash", str(wrapper_link)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )

        self.assertEqual(result.returncode, 3)
        self.assertIn("missing executable keystrel-client", result.stderr)


if __name__ == "__main__":
    unittest.main()
