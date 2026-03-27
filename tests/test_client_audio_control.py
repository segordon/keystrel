import os
import signal
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from tests._module_loader import load_client_module


keystrel_client = load_client_module()


def _args(**overrides):
    base = {
        "mute_output": True,
        "verbose": False,
        "start_chime": True,
        "chime_backend": "auto",
        "chime_cooldown_ms": 0,
        "device": None,
        "channels": 1,
        "sample_rate": 16000,
        "recover_output_mute": False,
        "mute_transaction_file": "/tmp/keystrel-test-mute-transaction-audio.json",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _record_args(**overrides):
    base = {
        "max_seconds": 0.5,
        "min_seconds": 0.0,
        "silence_seconds": 0.1,
        "threshold": 0.02,
        "block_seconds": 0.1,
        "pre_roll_seconds": 0.0,
        "start_speech_chunks": 1,
        "speech_ratio": 0.6,
        "noise_multiplier": 2.5,
        "webrtcvad": True,
        "webrtcvad_mode": 2,
        "webrtcvad_frame_ms": 20,
    }
    base.update(overrides)
    return _args(**base)


class _PreloadedQueue:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def get(self, timeout=None):  # noqa: ARG002
        if self._chunks:
            return self._chunks.pop(0)
        raise keystrel_client.queue.Empty

    def put(self, item):
        self._chunks.append(item)


class _NoopInputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ARG002
        return False


class _TimeoutCapturingEmptyQueue:
    def __init__(self, timeouts):
        self._timeouts = timeouts

    def get(self, timeout=None):
        self._timeouts.append(timeout)
        raise keystrel_client.queue.Empty

    def put(self, item):  # noqa: ARG002
        return None


class ClientParseAndPactlTests(unittest.TestCase):
    def test_list_output_sinks_parses_tab_and_space_formats(self):
        fake_result = SimpleNamespace(
            returncode=0,
            stdout="1\talsa_output.a\n2 alsa_output.b\n\n",
            stderr="",
        )
        with mock.patch.object(keystrel_client.subprocess, "run", return_value=fake_result):
            sinks = keystrel_client.list_output_sinks()

        self.assertEqual(sinks, ["1", "2"])

    def test_list_output_sinks_raises_on_pactl_failure(self):
        fake_result = SimpleNamespace(returncode=1, stdout="", stderr="pactl failed")
        with mock.patch.object(keystrel_client.subprocess, "run", return_value=fake_result):
            with self.assertRaisesRegex(RuntimeError, "pactl failed"):
                keystrel_client.list_output_sinks()

    def test_get_sink_mute_state_handles_yes_no_and_errors(self):
        yes_result = SimpleNamespace(returncode=0, stdout="Mute: yes\n", stderr="")
        no_result = SimpleNamespace(returncode=0, stdout="Mute: no\n", stderr="")
        weird_result = SimpleNamespace(returncode=0, stdout="Mute: maybe\n", stderr="")
        err_result = SimpleNamespace(returncode=1, stdout="", stderr="boom")

        with mock.patch.object(keystrel_client.subprocess, "run", return_value=yes_result):
            self.assertTrue(keystrel_client.get_sink_mute_state("1"))
        with mock.patch.object(keystrel_client.subprocess, "run", return_value=no_result):
            self.assertFalse(keystrel_client.get_sink_mute_state("1"))
        with mock.patch.object(keystrel_client.subprocess, "run", return_value=weird_result):
            with self.assertRaisesRegex(RuntimeError, "unexpected pactl output"):
                keystrel_client.get_sink_mute_state("1")
        with mock.patch.object(keystrel_client.subprocess, "run", return_value=err_result):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                keystrel_client.get_sink_mute_state("1")

    def test_run_pactl_retries_timeout_then_succeeds(self):
        timeout_exc = keystrel_client.subprocess.TimeoutExpired(cmd=["pactl", "info"], timeout=0.1)
        ok_result = SimpleNamespace(returncode=0, stdout="Server Name: PulseAudio\n", stderr="")

        with (
            mock.patch.object(
                keystrel_client.subprocess,
                "run",
                side_effect=[timeout_exc, ok_result],
            ) as run,
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
        ):
            result = keystrel_client._run_pactl(["info"], timeout_s=0.1, retries=1)

        self.assertEqual(result, ok_result)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()

    def test_run_pactl_raises_after_retry_budget_exhausted(self):
        timeout_exc = keystrel_client.subprocess.TimeoutExpired(cmd=["pactl", "info"], timeout=0.05)
        with (
            mock.patch.object(keystrel_client.subprocess, "run", side_effect=timeout_exc) as run,
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                keystrel_client._run_pactl(["info"], timeout_s=0.05, retries=2)

        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


class ClientVadAndSpeechRatioTests(unittest.TestCase):
    def test_build_webrtc_vad_branches(self):
        args = _args(webrtcvad=True, webrtcvad_mode=2, webrtcvad_frame_ms=20)

        with mock.patch.object(keystrel_client, "webrtcvad", None):
            self.assertIsNone(keystrel_client.build_webrtc_vad(args))

        with mock.patch.object(keystrel_client, "webrtcvad", SimpleNamespace(Vad=lambda mode: ("vad", mode))):
            bad_rate = _args(webrtcvad=True, sample_rate=11025, webrtcvad_mode=2, webrtcvad_frame_ms=20)
            self.assertIsNone(keystrel_client.build_webrtc_vad(bad_rate))

            bad_frame = _args(webrtcvad=True, sample_rate=16000, webrtcvad_mode=2, webrtcvad_frame_ms=15)
            self.assertIsNone(keystrel_client.build_webrtc_vad(bad_frame))

            ok = _args(webrtcvad=True, sample_rate=16000, webrtcvad_mode=3, webrtcvad_frame_ms=20)
            self.assertEqual(keystrel_client.build_webrtc_vad(ok), ("vad", 3))

        class _RaisingVadModule:
            @staticmethod
            def Vad(_mode):
                raise RuntimeError("init fail")

        with mock.patch.object(keystrel_client, "webrtcvad", _RaisingVadModule):
            self.assertIsNone(keystrel_client.build_webrtc_vad(args))

        disabled = _args(webrtcvad=False, sample_rate=16000, webrtcvad_mode=2, webrtcvad_frame_ms=20)
        self.assertIsNone(keystrel_client.build_webrtc_vad(disabled))

    def test_speech_ratio_in_chunk_paths(self):
        args = _args(sample_rate=16000, webrtcvad_frame_ms=20)

        self.assertIsNone(keystrel_client.speech_ratio_in_chunk(np.zeros((320, 1), dtype=np.float32), args, None))

        short_ratio = keystrel_client.speech_ratio_in_chunk(np.zeros((10, 1), dtype=np.float32), args, object())
        self.assertEqual(short_ratio, 0.0)

        class _FakeVad:
            def __init__(self):
                self._values = [True, False]

            def is_speech(self, *_args):
                return self._values.pop(0)

        chunk = np.ones((640, 2), dtype=np.float32) * 0.1
        ratio = keystrel_client.speech_ratio_in_chunk(chunk, args, _FakeVad())
        self.assertEqual(ratio, 0.5)

        class _RaisingVad:
            def is_speech(self, *_args):
                raise RuntimeError("vad error")

        self.assertIsNone(keystrel_client.speech_ratio_in_chunk(chunk, args, _RaisingVad()))


class ClientChimeBackendTests(unittest.TestCase):
    def test_play_chime_paplay_success_and_failure_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            chime_file = Path(tmp_dir) / "bell.oga"
            chime_file.write_bytes(b"dummy")
            args = _args(chime_file=str(chime_file), chime_sink="sink0", verbose=True)

            ok_result = SimpleNamespace(returncode=0, stderr="")
            with (
                mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/paplay"),
                mock.patch.object(keystrel_client.subprocess, "run", return_value=ok_result) as run_cmd,
            ):
                self.assertTrue(keystrel_client._play_chime_paplay(args))

            cmd = run_cmd.call_args.args[0]
            self.assertEqual(cmd[0], "paplay")
            self.assertIn("--device", cmd)
            self.assertIn("sink0", cmd)

            fail_result = SimpleNamespace(returncode=1, stderr="no output")
            with (
                mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/paplay"),
                mock.patch.object(keystrel_client.subprocess, "run", return_value=fail_result),
            ):
                self.assertFalse(keystrel_client._play_chime_paplay(args))

            with (
                mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/paplay"),
                mock.patch.object(keystrel_client.subprocess, "run", side_effect=RuntimeError("oops")),
            ):
                self.assertFalse(keystrel_client._play_chime_paplay(args))

    def test_play_chime_pipewire_canberra_and_sounddevice_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            chime_file = Path(tmp_dir) / "bell.oga"
            chime_file.write_bytes(b"dummy")

            pipe_args = _args(
                chime_file=str(chime_file),
                chime_target="target0",
                chime_role="Music",
                chime_volume=0.4,
                verbose=True,
            )
            ok_result = SimpleNamespace(returncode=0, stderr="")
            with (
                mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/pw-play"),
                mock.patch.object(keystrel_client.subprocess, "run", return_value=ok_result) as run_cmd,
            ):
                self.assertTrue(keystrel_client._play_chime_pipewire(pipe_args))
            self.assertIn("--target", run_cmd.call_args.args[0])

            can_args = _args(chime_file=str(Path(tmp_dir) / "missing.oga"), chime_event_id="bell")
            with (
                mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/canberra-gtk-play"),
                mock.patch.object(
                    keystrel_client.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stderr=""),
                ) as run_canberra,
            ):
                self.assertTrue(keystrel_client._play_chime_canberra(can_args))
            self.assertIn("-i", run_canberra.call_args.args[0])

            sd_args = _args(chime_duration_ms=50, chime_freq_hz=2200.0, chime_volume=0.25, verbose=True)
            with (
                mock.patch.object(keystrel_client.sd, "play", return_value=None, create=True) as play,
                mock.patch.object(keystrel_client.sd, "stop", return_value=None, create=True) as stop,
            ):
                self.assertTrue(keystrel_client._play_chime_sounddevice(sd_args))
            play.assert_called_once()
            stop.assert_called_once()

            with (
                mock.patch.object(keystrel_client.sd, "play", side_effect=RuntimeError("boom"), create=True),
                mock.patch.object(keystrel_client.sd, "stop", return_value=None, create=True) as stop,
            ):
                self.assertFalse(keystrel_client._play_chime_sounddevice(sd_args))
            stop.assert_called_once()

    def test_play_start_chime_paplay_backend_and_verbose_failure_message(self):
        args = _args(chime_backend="paplay", verbose=True)
        with (
            mock.patch.object(keystrel_client, "_play_chime_paplay", return_value=False) as paplay,
            mock.patch.object(keystrel_client, "_play_chime_pipewire", return_value=False) as pipewire,
            mock.patch.object(keystrel_client, "_play_chime_sounddevice", return_value=False) as sounddevice,
            mock.patch.object(keystrel_client, "_play_chime_canberra", return_value=False) as canberra,
            mock.patch("sys.stderr", new_callable=mock.MagicMock) as _stderr,
        ):
            keystrel_client.play_start_chime(args)

        paplay.assert_called_once()
        pipewire.assert_called_once()
        sounddevice.assert_called_once()
        canberra.assert_called_once()


class ClientMuteControlTests(unittest.TestCase):
    def test_mute_output_disabled_returns_empty(self):
        args = _args(mute_output=False)
        self.assertEqual(keystrel_client.mute_output_during_capture(args), {})

    def test_mute_output_missing_pactl_returns_empty(self):
        args = _args()
        with mock.patch.object(keystrel_client.shutil, "which", return_value=None):
            self.assertEqual(keystrel_client.mute_output_during_capture(args), {})

    def test_mute_output_sets_only_unmuted_sinks(self):
        args = _args()
        with (
            mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/pactl"),
            mock.patch.object(keystrel_client, "list_output_sinks", return_value=["1", "2"]),
            mock.patch.object(
                keystrel_client,
                "list_output_sink_details",
                return_value=[
                    {"sink": "1", "name": "sink.one"},
                    {"sink": "2", "name": "sink.two"},
                ],
            ),
            mock.patch.object(keystrel_client, "get_sink_mute_state", side_effect=[False, True]),
            mock.patch.object(keystrel_client, "set_sink_mute_state") as set_mute,
        ):
            states = keystrel_client.mute_output_during_capture(args)

        self.assertEqual(states["1"]["was_muted"], False)
        self.assertEqual(states["1"]["name"], "sink.one")
        self.assertEqual(states["1"]["changed"], True)
        self.assertEqual(states["2"]["was_muted"], True)
        self.assertEqual(states["2"]["name"], "sink.two")
        self.assertEqual(states["2"]["changed"], False)
        set_mute.assert_called_once_with("1", True)

    def test_mute_output_returns_partial_state_on_error(self):
        args = _args(verbose=True)
        with (
            mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/pactl"),
            mock.patch.object(keystrel_client, "list_output_sinks", return_value=["1", "2"]),
            mock.patch.object(
                keystrel_client,
                "list_output_sink_details",
                return_value=[
                    {"sink": "1", "name": "sink.one"},
                    {"sink": "2", "name": "sink.two"},
                ],
            ),
            mock.patch.object(
                keystrel_client,
                "get_sink_mute_state",
                side_effect=[False, RuntimeError("boom")],
            ),
            mock.patch.object(keystrel_client, "set_sink_mute_state") as set_mute,
        ):
            states = keystrel_client.mute_output_during_capture(args)

        self.assertEqual(states["1"]["was_muted"], False)
        self.assertEqual(states["1"]["name"], "sink.one")
        self.assertEqual(states["1"]["changed"], True)
        self.assertEqual(len(states), 1)
        set_mute.assert_called_once_with("1", True)

    def test_restore_output_mute_attempts_all_sinks(self):
        args = _args(verbose=True)
        with mock.patch.object(
            keystrel_client,
            "set_sink_mute_state",
            side_effect=[RuntimeError("fail one"), None],
        ) as set_mute:
            keystrel_client.restore_output_mute(args, {"1": False, "2": True})

        self.assertEqual(set_mute.call_count, 2)
        set_mute.assert_any_call("1", False)
        set_mute.assert_any_call("2", True)

    def test_restore_output_mute_remaps_changed_sink_by_name(self):
        args = _args(verbose=True)
        sink_states = {
            "1": {
                "sink": "1",
                "name": "sink.alpha",
                "was_muted": False,
                "changed": True,
            }
        }
        with (
            mock.patch.object(
                keystrel_client,
                "list_output_sink_details",
                return_value=[{"sink": "9", "name": "sink.alpha"}],
            ),
            mock.patch.object(
                keystrel_client,
                "set_sink_mute_state",
                return_value=None,
            ) as set_mute,
        ):
            unresolved = keystrel_client.restore_output_mute(args, sink_states)

        self.assertEqual(unresolved, {})
        self.assertEqual(set_mute.call_count, 1)
        self.assertEqual(set_mute.call_args_list[0].args, ("9", False))

    def test_restore_output_mute_avoids_reused_sink_id_when_name_mismatch(self):
        args = _args(verbose=False)
        sink_states = {
            "1": {
                "sink": "1",
                "name": "sink.alpha",
                "was_muted": False,
                "changed": True,
            }
        }
        with (
            mock.patch.object(
                keystrel_client,
                "list_output_sink_details",
                return_value=[
                    {"sink": "1", "name": "sink.other"},
                    {"sink": "9", "name": "sink.alpha"},
                ],
            ),
            mock.patch.object(
                keystrel_client,
                "set_sink_mute_state",
                return_value=None,
            ) as set_mute,
        ):
            unresolved = keystrel_client.restore_output_mute(args, sink_states)

        self.assertEqual(unresolved, {})
        self.assertEqual(set_mute.call_count, 1)
        self.assertEqual(set_mute.call_args_list[0].args, ("9", False))

    def test_restore_output_mute_returns_unresolved_state_records(self):
        args = _args(verbose=False)
        sink_states = {
            "3": {
                "sink": "3",
                "name": "sink.beta",
                "was_muted": False,
                "changed": True,
            }
        }
        with (
            mock.patch.object(keystrel_client, "list_output_sink_details", return_value=[]),
            mock.patch.object(
                keystrel_client,
                "set_sink_mute_state",
                side_effect=RuntimeError("still gone"),
            ),
        ):
            unresolved = keystrel_client.restore_output_mute(args, sink_states)

        self.assertIn("3", unresolved)
        self.assertEqual(unresolved["3"]["name"], "sink.beta")
        self.assertEqual(unresolved["3"]["was_muted"], False)
        self.assertEqual(unresolved["3"]["changed"], True)


class ClientMuteConfirmationTests(unittest.TestCase):
    def test_confirm_output_mute_returns_when_all_sinks_muted(self):
        args = _args(mute_settle_ms=300)
        with (
            mock.patch.object(
                keystrel_client,
                "get_sink_mute_state",
                side_effect=[False, True],
            ) as get_mute,
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
            mock.patch.object(
                keystrel_client.time,
                "monotonic",
                side_effect=[0.0, 0.01, 0.02, 0.03, 0.04],
            ),
        ):
            keystrel_client.confirm_output_mute_before_capture(args, {"1": False, "2": True})

        self.assertEqual(get_mute.call_count, 2)
        sleep.assert_called_once_with(0.02)

    def test_confirm_output_mute_times_out(self):
        args = _args(mute_settle_ms=40, verbose=True)
        with (
            mock.patch.object(keystrel_client, "get_sink_mute_state", return_value=False) as get_mute,
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
            mock.patch.object(
                keystrel_client.time,
                "monotonic",
                side_effect=[0.0, 0.01, 0.03, 0.05, 0.05],
            ),
        ):
            keystrel_client.confirm_output_mute_before_capture(args, {"1": False})

        self.assertEqual(get_mute.call_count, 1)
        self.assertEqual(sleep.call_count, 1)
        self.assertAlmostEqual(sleep.call_args_list[0].args[0], 0.01)

    def test_confirm_output_mute_stops_checking_extra_sinks_after_deadline(self):
        args = _args(mute_settle_ms=20, verbose=False)
        with (
            mock.patch.object(keystrel_client, "get_sink_mute_state", return_value=False) as get_mute,
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
            mock.patch.object(
                keystrel_client.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.03, 0.03],
            ),
        ):
            keystrel_client.confirm_output_mute_before_capture(
                args,
                {
                    "1": {"sink": "1", "name": "sink.one", "was_muted": False, "changed": True},
                    "2": {"sink": "2", "name": "sink.two", "was_muted": False, "changed": True},
                },
            )

        self.assertEqual(get_mute.call_count, 1)
        sleep.assert_not_called()

    def test_confirm_output_mute_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cancel_file = Path(tmp_dir) / "cancel.flag"
            cancel_file.write_text("1", encoding="utf-8")
            args = _args(mute_settle_ms=300, cancel_file=str(cancel_file))

            with mock.patch.object(keystrel_client, "get_sink_mute_state") as get_mute:
                with self.assertRaises(keystrel_client.CaptureCancelled):
                    keystrel_client.confirm_output_mute_before_capture(args, {"1": False})

        get_mute.assert_not_called()


class ClientMuteRecoveryTests(unittest.TestCase):
    def test_recover_stale_output_mute_clears_transaction_on_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            transaction_path = Path(tmp_dir) / "mute-transaction.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sinks": [
                            {
                                "sink": "1",
                                "name": "sink.alpha",
                                "was_muted": False,
                                "changed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = _args(mute_transaction_file=str(transaction_path), verbose=True)

            with (
                mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/pactl"),
                mock.patch.object(keystrel_client, "restore_output_mute", return_value={}) as restore,
            ):
                recovered = keystrel_client.recover_stale_output_mute(args, force=True)

        self.assertTrue(recovered)
        restore.assert_called_once()
        self.assertFalse(transaction_path.exists())

    def test_recover_stale_output_mute_keeps_transaction_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            transaction_path = Path(tmp_dir) / "mute-transaction.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sinks": [
                            {
                                "sink": "1",
                                "name": "sink.alpha",
                                "was_muted": False,
                                "changed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = _args(mute_transaction_file=str(transaction_path), verbose=False)
            unresolved = {
                "1": {
                    "sink": "1",
                    "name": "sink.alpha",
                    "was_muted": False,
                    "changed": True,
                }
            }

            with (
                mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/pactl"),
                mock.patch.object(
                    keystrel_client,
                    "restore_output_mute",
                    return_value=unresolved,
                ) as restore,
            ):
                recovered = keystrel_client.recover_stale_output_mute(args, force=False)

            self.assertFalse(recovered)
            self.assertTrue(transaction_path.exists())
            restore.assert_called_once()

    def test_signal_handler_triggers_mute_cleanup(self):
        args = _args(verbose=False)
        sink_states = {
            "1": {
                "sink": "1",
                "name": "sink.alpha",
                "was_muted": False,
                "changed": True,
            }
        }
        cleanup_state = {"done": False, "in_progress": False}

        with mock.patch.object(keystrel_client, "_finalize_output_mute_cleanup") as finalize:
            previous = keystrel_client._install_output_restore_signal_handlers(
                args,
                sink_states,
                cleanup_state,
            )
            try:
                handler = signal.getsignal(signal.SIGTERM)
                self.assertTrue(callable(handler))
                if not callable(handler):
                    self.fail("SIGTERM handler was not installed")
                with self.assertRaises(SystemExit) as ctx:
                    handler(signal.SIGTERM, None)
            finally:
                keystrel_client._restore_previous_signal_handlers(previous)

        self.assertEqual(ctx.exception.code, 128 + signal.SIGTERM)
        finalize.assert_called_once()


class ClientInputDeviceSelectionTests(unittest.TestCase):
    def test_auto_select_keeps_explicit_device(self):
        args = _args(device="UM10")
        selected, auto_selected = keystrel_client.auto_select_input_device(args)
        self.assertEqual(selected, "UM10")
        self.assertFalse(auto_selected)

    def test_auto_select_uses_input_only_when_default_virtual(self):
        args = _args(verbose=True, device=None)
        devices = [
            {"name": "hdmi", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "UM10: USB Audio", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "default", "max_input_channels": 64, "max_output_channels": 64},
        ]
        with (
            mock.patch.object(keystrel_client.sd, "query_devices", return_value=devices, create=True),
            mock.patch.object(
                keystrel_client.sd,
                "default",
                SimpleNamespace(device=[2, 2]),
                create=True,
            ),
            mock.patch.object(keystrel_client.sd, "check_input_settings", return_value=None, create=True),
        ):
            selected, auto_selected = keystrel_client.auto_select_input_device(args)

        self.assertEqual(selected, 1)
        self.assertTrue(auto_selected)

    def test_auto_select_skips_when_default_is_dedicated_input(self):
        args = _args(device=None)
        devices = [
            {"name": "UM10: USB Audio", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "default", "max_input_channels": 64, "max_output_channels": 64},
        ]
        with (
            mock.patch.object(keystrel_client.sd, "query_devices", return_value=devices, create=True),
            mock.patch.object(
                keystrel_client.sd,
                "default",
                SimpleNamespace(device=[0, 0]),
                create=True,
            ),
        ):
            selected, auto_selected = keystrel_client.auto_select_input_device(args)

        self.assertIsNone(selected)
        self.assertFalse(auto_selected)

    def test_auto_select_skips_candidate_with_unsupported_sample_rate(self):
        args = _args(device=None)
        devices = [
            {"name": "UM10: USB Audio", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "default", "max_input_channels": 64, "max_output_channels": 64},
        ]
        with (
            mock.patch.object(keystrel_client.sd, "query_devices", return_value=devices, create=True),
            mock.patch.object(
                keystrel_client.sd,
                "default",
                SimpleNamespace(device=[1, 1]),
                create=True,
            ),
            mock.patch.object(
                keystrel_client.sd,
                "check_input_settings",
                side_effect=RuntimeError("unsupported"),
                create=True,
            ),
        ):
            selected, auto_selected = keystrel_client.auto_select_input_device(args)

        self.assertIsNone(selected)
        self.assertFalse(auto_selected)

    def test_auto_select_handles_single_default_device_int(self):
        args = _args(verbose=True, device=None)
        devices = [
            {"name": "hdmi", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "UM10: USB Audio", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "default", "max_input_channels": 64, "max_output_channels": 64},
        ]
        with (
            mock.patch.object(keystrel_client.sd, "query_devices", return_value=devices, create=True),
            mock.patch.object(
                keystrel_client.sd,
                "default",
                SimpleNamespace(device=2),
                create=True,
            ),
            mock.patch.object(keystrel_client.sd, "check_input_settings", return_value=None, create=True),
        ):
            selected, auto_selected = keystrel_client.auto_select_input_device(args)

        self.assertEqual(selected, 1)
        self.assertTrue(auto_selected)


class ClientRecordUntilSilenceTests(unittest.TestCase):
    def test_record_until_silence_returns_empty_when_voice_never_starts(self):
        args = _record_args(max_seconds=0.45, threshold=1.0, pre_roll_seconds=0.0)
        chunk = np.zeros((1600, 1), dtype=np.float32)

        monotonic_values = iter([0.0, 0.12, 0.24, 0.36, 0.48])

        with (
            mock.patch.object(keystrel_client, "build_webrtc_vad", return_value=None),
            mock.patch.object(
                keystrel_client.queue,
                "Queue",
                side_effect=lambda: _PreloadedQueue([chunk, chunk]),
            ),
            mock.patch.object(keystrel_client.sd, "InputStream", _NoopInputStream, create=True),
            mock.patch.object(keystrel_client.time, "monotonic", side_effect=lambda: next(monotonic_values)),
        ):
            audio = keystrel_client.record_until_silence(args)

        self.assertEqual(audio.size, 0)

    def test_record_until_silence_keeps_preroll_and_stops_on_trailing_silence(self):
        args = _record_args(
            pre_roll_seconds=0.2,
            min_seconds=0.0,
            silence_seconds=0.1,
            start_speech_chunks=1,
        )
        quiet = np.zeros((1600, 1), dtype=np.float32)
        voice = np.full((1600, 1), 0.4, dtype=np.float32)

        monotonic_values = iter([0.0, 0.10, 0.20, 0.36])

        with (
            mock.patch.object(keystrel_client, "build_webrtc_vad", return_value=object()),
            mock.patch.object(
                keystrel_client,
                "speech_ratio_in_chunk",
                side_effect=[0.0, 1.0, 0.0],
            ),
            mock.patch.object(
                keystrel_client.queue,
                "Queue",
                side_effect=lambda: _PreloadedQueue([quiet, voice, quiet]),
            ),
            mock.patch.object(keystrel_client.sd, "InputStream", _NoopInputStream, create=True),
            mock.patch.object(keystrel_client.time, "monotonic", side_effect=lambda: next(monotonic_values)),
        ):
            audio = keystrel_client.record_until_silence(args)

        self.assertEqual(audio.shape, (4800, 1))
        self.assertTrue(np.allclose(audio[0:1600], quiet))
        self.assertTrue(np.allclose(audio[1600:3200], voice))

    def test_record_until_silence_raises_on_cancel_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cancel_file = Path(tmp_dir) / "cancel.flag"
            cancel_file.write_text("1", encoding="utf-8")
            args = _record_args(cancel_file=str(cancel_file))

            monotonic_values = iter([0.0, 0.01])

            with (
                mock.patch.object(keystrel_client, "build_webrtc_vad", return_value=None),
                mock.patch.object(
                    keystrel_client.queue,
                    "Queue",
                    side_effect=lambda: _PreloadedQueue([]),
                ),
                mock.patch.object(keystrel_client.sd, "InputStream", _NoopInputStream, create=True),
                mock.patch.object(keystrel_client.time, "monotonic", side_effect=lambda: next(monotonic_values)),
            ):
                with self.assertRaises(keystrel_client.CaptureCancelled):
                    keystrel_client.record_until_silence(args)

    def test_record_until_silence_uses_short_poll_timeout(self):
        args = _record_args(max_seconds=0.11, block_seconds=0.2, threshold=1.0, pre_roll_seconds=0.0)
        monotonic_values = iter([0.0, 0.03, 0.07, 0.12])
        captured_timeouts = []

        with (
            mock.patch.object(keystrel_client, "build_webrtc_vad", return_value=None),
            mock.patch.object(
                keystrel_client.queue,
                "Queue",
                side_effect=lambda: _TimeoutCapturingEmptyQueue(captured_timeouts),
            ),
            mock.patch.object(keystrel_client.sd, "InputStream", _NoopInputStream, create=True),
            mock.patch.object(keystrel_client.time, "monotonic", side_effect=lambda: next(monotonic_values)),
        ):
            audio = keystrel_client.record_until_silence(args)

        self.assertEqual(audio.size, 0)
        self.assertGreaterEqual(len(captured_timeouts), 1)
        self.assertTrue(all(timeout <= 0.05 for timeout in captured_timeouts if timeout is not None))


class ClientChimeSelectionTests(unittest.TestCase):
    def test_play_start_chime_skips_when_disabled(self):
        args = _args(start_chime=False)
        with (
            mock.patch.object(keystrel_client, "_play_chime_pipewire") as pipewire,
            mock.patch.object(keystrel_client, "_play_chime_paplay") as paplay,
            mock.patch.object(keystrel_client, "_play_chime_sounddevice") as sounddevice,
            mock.patch.object(keystrel_client, "_play_chime_canberra") as canberra,
        ):
            keystrel_client.play_start_chime(args)

        pipewire.assert_not_called()
        paplay.assert_not_called()
        sounddevice.assert_not_called()
        canberra.assert_not_called()

    def test_play_start_chime_auto_stops_at_first_success(self):
        args = _args(chime_backend="auto")
        with (
            mock.patch.object(keystrel_client, "_play_chime_pipewire", return_value=True) as pipewire,
            mock.patch.object(keystrel_client, "_play_chime_paplay", return_value=False) as paplay,
            mock.patch.object(keystrel_client, "_play_chime_sounddevice", return_value=False) as sounddevice,
            mock.patch.object(keystrel_client, "_play_chime_canberra", return_value=False) as canberra,
        ):
            keystrel_client.play_start_chime(args)

        pipewire.assert_called_once()
        paplay.assert_not_called()
        sounddevice.assert_not_called()
        canberra.assert_not_called()

    def test_play_start_chime_pipewire_falls_back_to_paplay(self):
        args = _args(chime_backend="pipewire")
        with (
            mock.patch.object(keystrel_client, "_play_chime_pipewire", return_value=False) as pipewire,
            mock.patch.object(keystrel_client, "_play_chime_paplay", return_value=True) as paplay,
            mock.patch.object(keystrel_client, "_play_chime_sounddevice", return_value=False) as sounddevice,
            mock.patch.object(keystrel_client, "_play_chime_canberra", return_value=False) as canberra,
        ):
            keystrel_client.play_start_chime(args)

        pipewire.assert_called_once()
        paplay.assert_called_once()
        sounddevice.assert_not_called()
        canberra.assert_not_called()

    def test_play_start_chime_canberra_then_fallbacks(self):
        args = _args(chime_backend="canberra")
        with (
            mock.patch.object(keystrel_client, "_play_chime_canberra", return_value=False) as canberra,
            mock.patch.object(keystrel_client, "_play_chime_pipewire", return_value=False) as pipewire,
            mock.patch.object(keystrel_client, "_play_chime_paplay", return_value=True) as paplay,
            mock.patch.object(keystrel_client, "_play_chime_sounddevice", return_value=False) as sounddevice,
        ):
            keystrel_client.play_start_chime(args)

        canberra.assert_called_once()
        pipewire.assert_called_once()
        paplay.assert_called_once()
        sounddevice.assert_not_called()

    def test_play_start_chime_applies_cooldown_sleep(self):
        args = _args(chime_backend="auto", chime_cooldown_ms=25)
        with (
            mock.patch.object(keystrel_client, "_play_chime_pipewire", return_value=False),
            mock.patch.object(keystrel_client, "_play_chime_paplay", return_value=False),
            mock.patch.object(keystrel_client, "_play_chime_sounddevice", return_value=False),
            mock.patch.object(keystrel_client, "_play_chime_canberra", return_value=False),
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
        ):
            keystrel_client.play_start_chime(args)

        sleep.assert_called_once_with(0.025)


class ClientLockTests(unittest.TestCase):
    def test_acquire_client_lock_is_nonblocking(self):
        args = _args(verbose=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = os.path.join(tmp_dir, "client.lock")
            with mock.patch.dict(os.environ, {"KEYSTREL_CLIENT_LOCK": lock_path}, clear=False):
                first = keystrel_client.acquire_client_lock(args)
                self.assertIsNotNone(first)

                second = keystrel_client.acquire_client_lock(args)
                self.assertIsNone(second)

                first.close()
                third = keystrel_client.acquire_client_lock(args)
                self.assertIsNotNone(third)
                third.close()


if __name__ == "__main__":
    unittest.main()
