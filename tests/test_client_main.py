import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

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
        "channels": 1,
        "start_chime": False,
        "cancel_file": "",
        "recover_output_mute": False,
        "mute_transaction_file": f"/tmp/keystrel-test-mute-transaction-main-{uuid4().hex}.json",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ClientMainFlowTests(unittest.TestCase):
    def test_lock_unavailable_returns_without_side_effects(self):
        args = _base_args()
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client, "acquire_client_lock", return_value=None),
            mock.patch.object(keystrel_client, "parse_server_endpoint") as parse_server,
        ):
            keystrel_client.main()

        parse_server.assert_not_called()

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

    def test_recover_output_mute_mode_exits_when_lock_unavailable(self):
        args = _base_args(recover_output_mute=True)
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client, "acquire_client_lock", return_value=None),
        ):
            with self.assertRaises(SystemExit) as ctx:
                keystrel_client.main()

        self.assertEqual(ctx.exception.code, 6)

    def test_recover_output_mute_mode_returns_without_capture(self):
        args = _base_args(recover_output_mute=True)
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
            mock.patch.object(keystrel_client, "recover_stale_output_mute", return_value=True) as recover,
            mock.patch.object(keystrel_client, "_resolve_transcription_target") as resolve_target,
        ):
            keystrel_client.main()

        recover.assert_called_once_with(args, force=True)
        resolve_target.assert_not_called()

    def test_recover_output_mute_mode_exits_when_recovery_fails(self):
        args = _base_args(recover_output_mute=True)
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
            mock.patch.object(keystrel_client, "recover_stale_output_mute", return_value=False),
        ):
            with self.assertRaises(SystemExit) as ctx:
                keystrel_client.main()

        self.assertEqual(ctx.exception.code, 6)

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

    def test_invalid_remote_server_config_exits_with_code_2(self):
        args = _base_args(server="bad")
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
            mock.patch.object(keystrel_client, "parse_server_endpoint", side_effect=ValueError("bad endpoint")),
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

    def test_remote_success_prints_json_and_uses_tcp_transport(self):
        args = _base_args(server="tcp://127.0.0.1:8765", server_token="secret", json=True, verbose=True)

        def _fake_write(path, audio, sample_rate):  # noqa: ARG001
            Path(path).write_bytes(b"fake-wav")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(keystrel_client, "parse_args", return_value=args),
            mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
            mock.patch.object(keystrel_client, "parse_server_endpoint", return_value=("127.0.0.1", 8765)),
            mock.patch.object(keystrel_client, "play_start_chime"),
            mock.patch.object(keystrel_client, "record_until_silence", return_value=_FakeAudio(1)),
            mock.patch.object(keystrel_client.sf, "write", side_effect=_fake_write, create=True),
            mock.patch.object(
                keystrel_client,
                "send_tcp_request",
                return_value={"ok": True, "text": "remote text", "elapsed_s": 0.42, "language": "en"},
            ) as send_tcp,
            mock.patch.object(keystrel_client, "send_unix_request") as send_unix,
            mock.patch("sys.stdout", new=stdout),
            mock.patch("sys.stderr", new=stderr),
        ):
            keystrel_client.main()

        send_unix.assert_not_called()
        send_tcp.assert_called_once()
        payload = send_tcp.call_args.args[2]
        self.assertIn("audio_b64", payload)
        self.assertEqual(payload["auth_token"], "secret")
        self.assertIn('"text": "remote text"', stdout.getvalue())
        self.assertIn("elapsed=0.42s", stderr.getvalue())

    def test_local_success_prints_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(socket=str(socket_path), verbose=True)

            def _fake_write(path, audio, sample_rate):  # noqa: ARG001
                Path(path).write_bytes(b"fake-wav")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "record_until_silence", return_value=_FakeAudio(1)),
                mock.patch.object(keystrel_client.sf, "write", side_effect=_fake_write, create=True),
                mock.patch.object(
                    keystrel_client,
                    "send_unix_request",
                    return_value={"ok": True, "text": "local text", "elapsed_s": 0.31, "language": "en"},
                ) as send_unix,
                mock.patch("sys.stdout", new=stdout),
                mock.patch("sys.stderr", new=stderr),
            ):
                keystrel_client.main()

        send_unix.assert_called_once()
        self.assertEqual(stdout.getvalue(), "local text\n")
        self.assertIn("elapsed=0.31s", stderr.getvalue())

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

    def test_cancel_after_chime_skips_capture_and_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            cancel_path = Path(tmp_dir) / "cancel.flag"
            args = _base_args(socket=str(socket_path), cancel_file=str(cancel_path))

            def _chime_side_effect(_args):
                cancel_path.write_text("1", encoding="utf-8")

            stdout = io.StringIO()
            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime", side_effect=_chime_side_effect),
                mock.patch.object(keystrel_client, "record_until_silence") as record,
                mock.patch.object(keystrel_client, "send_unix_request") as send_unix,
                mock.patch("sys.stdout", new=stdout),
            ):
                keystrel_client.main()

        self.assertEqual(stdout.getvalue(), "")
        record.assert_not_called()
        send_unix.assert_not_called()

    def test_capture_cancelled_returns_empty_without_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            args = _base_args(socket=str(socket_path))

            stdout = io.StringIO()
            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(
                    keystrel_client,
                    "record_until_silence",
                    side_effect=keystrel_client.CaptureCancelled(),
                ),
                mock.patch.object(keystrel_client, "send_unix_request") as send_unix,
                mock.patch("sys.stdout", new=stdout),
            ):
                keystrel_client.main()

        self.assertEqual(stdout.getvalue(), "")
        send_unix.assert_not_called()

    def test_cancel_requested_after_capture_skips_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            cancel_path = Path(tmp_dir) / "cancel.flag"
            args = _base_args(socket=str(socket_path), cancel_file=str(cancel_path))

            def _record_and_cancel(_args, on_tick):  # noqa: ARG001
                cancel_path.write_text("1", encoding="utf-8")
                return _FakeAudio(123)

            stdout = io.StringIO()
            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "record_until_silence", side_effect=_record_and_cancel),
                mock.patch.object(keystrel_client.sf, "write", create=True) as sf_write,
                mock.patch.object(keystrel_client, "send_unix_request") as send_unix,
                mock.patch("sys.stdout", new=stdout),
            ):
                keystrel_client.main()

        self.assertEqual(stdout.getvalue(), "")
        sf_write.assert_not_called()
        send_unix.assert_not_called()

    def test_cancel_from_maybe_apply_mute_path_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "daemon.sock"
            socket_path.write_text("present", encoding="utf-8")
            cancel_path = Path(tmp_dir) / "cancel.flag"
            args = _base_args(
                socket=str(socket_path),
                cancel_file=str(cancel_path),
                mute_output=True,
                mute_start_delay_ms=120,
            )

            def _record_side_effect(_args, on_tick):
                cancel_path.write_text("1", encoding="utf-8")
                on_tick(0.20)
                return _FakeAudio(999)

            stdout = io.StringIO()
            with (
                mock.patch.object(keystrel_client, "parse_args", return_value=args),
                mock.patch.object(keystrel_client, "acquire_client_lock", return_value=io.StringIO()),
                mock.patch.object(keystrel_client, "play_start_chime"),
                mock.patch.object(keystrel_client, "mute_output_during_capture") as mute_now,
                mock.patch.object(keystrel_client, "record_until_silence", side_effect=_record_side_effect),
                mock.patch.object(keystrel_client, "send_unix_request") as send_unix,
                mock.patch("sys.stdout", new=stdout),
            ):
                keystrel_client.main()

        self.assertEqual(stdout.getvalue(), "")
        mute_now.assert_not_called()
        send_unix.assert_not_called()


if __name__ == "__main__":
    unittest.main()
