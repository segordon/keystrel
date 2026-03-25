import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._module_loader import load_client_module


keystrel_client = load_client_module()


class _FakeAudio:
    def __init__(self, size):
        self.size = size


def _base_args(**overrides):
    values = {
        "list_devices": False,
        "verbose": False,
        "server": "",
        "socket": "",
        "server_token": "",
        "mute_output": False,
        "mute_start_delay_ms": 0,
        "mute_settle_ms": 0,
        "socket_timeout": 1.0,
        "server_timeout": 1.0,
        "language": "",
        "vad_filter": None,
        "beam_size": None,
        "best_of": None,
        "json": False,
        "sample_rate": 16000,
        "start_chime": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ClientMainFlowTests(unittest.TestCase):
    def test_list_devices_returns_before_lock(self):
        args = _base_args(list_devices=True)
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client.sd, "query_devices", return_value=["mic0"], create=True),
            mock.patch.object(keystrel_client, "acquire_client_lock") as acquire_lock,
            mock.patch("sys.stdout", new=io.StringIO()) as stdout,
        ):
            keystrel_client.main()

        self.assertIn("mic0", stdout.getvalue())
        acquire_lock.assert_not_called()

    def test_missing_local_socket_exits_with_code_2(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = _base_args(socket=str(Path(tmp_dir) / "missing.sock"))
            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    keystrel_client.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_remote_mode_requires_token(self):
        args = _base_args(server="tcp://127.0.0.1:8765", server_token="")
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as ctx:
                keystrel_client.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_capture_failure_exits_with_code_3(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(socket=str(socket_path))

            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "record_until_silence", side_effect=RuntimeError("mic fail")),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    keystrel_client.main()

        self.assertEqual(ctx.exception.code, 3)

    def test_request_failure_exits_with_code_4(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(socket=str(socket_path))

            def _fake_write(path, audio, sample_rate):  # noqa: ARG001
                Path(path).write_bytes(b"fake-wav")

            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "record_until_silence", return_value=_FakeAudio(1)),
                mock.patch.object(keystrel_client.sf, "write", side_effect=_fake_write, create=True),
                mock.patch.object(keystrel_client, "send_unix_request", side_effect=RuntimeError("boom")),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    keystrel_client.main()

        self.assertEqual(ctx.exception.code, 4)

    def test_daemon_error_exits_with_code_5(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(socket=str(socket_path))

            def _fake_write(path, audio, sample_rate):  # noqa: ARG001
                Path(path).write_bytes(b"fake-wav")

            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "record_until_silence", return_value=_FakeAudio(1)),
                mock.patch.object(keystrel_client.sf, "write", side_effect=_fake_write, create=True),
                mock.patch.object(keystrel_client, "send_unix_request", return_value={"ok": False, "error": "bad"}),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    keystrel_client.main()

        self.assertEqual(ctx.exception.code, 5)

    def test_empty_audio_returns_without_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(socket=str(socket_path))

            stdout = io.StringIO()
            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "record_until_silence", return_value=_FakeAudio(0)),
                mock.patch.object(keystrel_client, "send_unix_request") as send_unix,
                mock.patch("sys.stdout", new=stdout),
            ):
                keystrel_client.main()

        self.assertEqual(stdout.getvalue(), "")
        send_unix.assert_not_called()

    def test_mute_confirmation_runs_before_capture(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(socket=str(socket_path), mute_output=True, mute_settle_ms=180)

            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "mute_output_during_capture", return_value={"1": False}),
                mock.patch.object(keystrel_client, "confirm_output_mute_before_capture") as confirm_mute,
                mock.patch.object(keystrel_client, "record_until_silence", return_value=_FakeAudio(0)),
            ):
                keystrel_client.main()

        confirm_mute.assert_called_once_with(args, {"1": False})

    def test_mute_start_delay_applies_mute_during_capture_tick(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(
                socket=str(socket_path),
                mute_output=True,
                mute_start_delay_ms=120,
                mute_settle_ms=300,
            )

            def _record_side_effect(_args, on_tick):
                on_tick(0.05)
                on_tick(0.15)
                return _FakeAudio(0)

            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "mute_output_during_capture", return_value={"1": False}) as mute_now,
                mock.patch.object(keystrel_client, "confirm_output_mute_before_capture") as confirm_mute,
                mock.patch.object(keystrel_client, "record_until_silence", side_effect=_record_side_effect),
            ):
                keystrel_client.main()

        mute_now.assert_called_once_with(args)
        confirm_mute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
