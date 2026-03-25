import os
import tempfile
import unittest
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
            mock.patch.object(keystrel_client, "get_sink_mute_state", side_effect=[False, True]),
            mock.patch.object(keystrel_client, "set_sink_mute_state") as set_mute,
        ):
            states = keystrel_client.mute_output_during_capture(args)

        self.assertEqual(states, {"1": False, "2": True})
        set_mute.assert_called_once_with("1", True)

    def test_mute_output_returns_partial_state_on_error(self):
        args = _args(verbose=True)
        with (
            mock.patch.object(keystrel_client.shutil, "which", return_value="/usr/bin/pactl"),
            mock.patch.object(keystrel_client, "list_output_sinks", return_value=["1", "2"]),
            mock.patch.object(
                keystrel_client,
                "get_sink_mute_state",
                side_effect=[False, RuntimeError("boom")],
            ),
            mock.patch.object(keystrel_client, "set_sink_mute_state") as set_mute,
        ):
            states = keystrel_client.mute_output_during_capture(args)

        self.assertEqual(states, {"1": False})
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


class ClientMuteConfirmationTests(unittest.TestCase):
    def test_confirm_output_mute_returns_when_all_sinks_muted(self):
        args = _args(mute_settle_ms=300)
        with (
            mock.patch.object(
                keystrel_client,
                "get_sink_mute_state",
                side_effect=[False, True, True],
            ) as get_mute,
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
            mock.patch.object(keystrel_client.time, "monotonic", side_effect=[0.0, 0.05, 0.10]),
        ):
            keystrel_client.confirm_output_mute_before_capture(args, {"1": False, "2": True})

        self.assertEqual(get_mute.call_count, 2)
        sleep.assert_called_once_with(0.02)

    def test_confirm_output_mute_times_out(self):
        args = _args(mute_settle_ms=40, verbose=True)
        with (
            mock.patch.object(keystrel_client, "get_sink_mute_state", return_value=False) as get_mute,
            mock.patch.object(keystrel_client.time, "sleep") as sleep,
            mock.patch.object(keystrel_client.time, "monotonic", side_effect=[0.0, 0.01, 0.03, 0.05]),
        ):
            keystrel_client.confirm_output_mute_before_capture(args, {"1": False})

        self.assertEqual(get_mute.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertAlmostEqual(sleep.call_args_list[0].args[0], 0.02)
        self.assertAlmostEqual(sleep.call_args_list[1].args[0], 0.01)


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
