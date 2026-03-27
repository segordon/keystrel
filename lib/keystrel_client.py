#!/usr/bin/env python3

import argparse
import base64
import fcntl
import json
import os
import queue
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
from collections import deque
from pathlib import Path

from keystrel_env import env_candidates as shared_env_candidates
from keystrel_env import get_env as shared_get_env
from keystrel_env import parse_bool as shared_parse_bool
from keystrel_env import parse_env_bool as shared_parse_env_bool
from keystrel_env import parse_env_choice as shared_parse_env_choice
from keystrel_env import parse_env_float as shared_parse_env_float
from keystrel_env import parse_env_int as shared_parse_env_int

import numpy as np
import sounddevice as sd  # type: ignore[import-not-found]
import soundfile as sf  # type: ignore[import-not-found]

try:
    import webrtcvad  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    webrtcvad = None


WEBRTCVAD_SAMPLE_RATES = {8000, 16000, 32000, 48000}
WEBRTCVAD_FRAME_MS = {10, 20, 30}

PACTL_CALL_TIMEOUT_S = 1.0
PACTL_RETRY_COUNT = 2
PACTL_RETRY_BASE_DELAY_S = 0.03
PACTL_RETRY_JITTER_S = 0.02
MUTE_TRANSACTION_VERSION = 1

_PACTL_TIMEOUT_OVERRIDE_S = None


class CaptureCancelled(Exception):
    pass


def parse_bool(value):
    return shared_parse_bool(value)


def _env_candidates(name):
    return shared_env_candidates(name)


_LEGACY_ENV_WARNED = set()


def get_env(name, default=None):
    return shared_get_env(name, default, _LEGACY_ENV_WARNED, "keystrel-client")


def parse_env_int(name, default):
    return shared_parse_env_int(name, default, get_env, "keystrel-client")


def parse_env_float(name, default):
    return shared_parse_env_float(name, default, get_env, "keystrel-client")


def parse_env_bool(name, default):
    return shared_parse_env_bool(name, default, get_env, "keystrel-client")


def parse_env_choice(name, default, choices):
    return shared_parse_env_choice(name, default, choices, get_env, "keystrel-client")


def normalize_audio_device(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        return text
    return value


def _add_transport_args(parser):
    parser.add_argument(
        "--socket",
        default=get_env("KEYSTREL_SOCKET", "~/.cache/keystrel/faster-whisper.sock"),
        help="Unix socket path",
    )
    parser.add_argument(
        "--server",
        default=get_env("KEYSTREL_SERVER", ""),
        help="optional remote server URL, e.g. tcp://<tailscale-ip>:8765",
    )
    parser.add_argument(
        "--server-token",
        default=get_env("KEYSTREL_SERVER_TOKEN", ""),
        help="shared auth token for remote server mode",
    )
    parser.add_argument(
        "--server-timeout",
        type=float,
        default=parse_env_float("KEYSTREL_SERVER_TIMEOUT", 30.0),
        help="seconds before remote server connect/read timeout",
    )


def _add_capture_args(parser):
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=parse_env_int("KEYSTREL_SAMPLE_RATE", 16000),
        help="input sample rate",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=1,
        help="number of input channels",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=parse_env_float("KEYSTREL_MAX_SECONDS", 12.0),
        help="hard capture limit",
    )
    parser.add_argument(
        "--min-seconds",
        type=float,
        default=parse_env_float("KEYSTREL_MIN_SECONDS", 0.35),
        help="minimum capture length before auto stop",
    )
    parser.add_argument(
        "--silence-seconds",
        type=float,
        default=parse_env_float("KEYSTREL_SILENCE_SECONDS", 0.9),
        help="auto stop after this much trailing silence",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=parse_env_float("KEYSTREL_THRESHOLD", 0.015),
        help="RMS threshold for voice activity",
    )
    parser.add_argument(
        "--block-seconds",
        type=float,
        default=0.08,
        help="capture block duration",
    )
    parser.add_argument(
        "--device",
        default=get_env("KEYSTREL_INPUT_DEVICE"),
        help="optional input device name/id",
    )


def _add_transcription_override_args(parser):
    parser.add_argument(
        "--language",
        default=get_env("KEYSTREL_LANGUAGE", ""),
        help="optional language code, e.g. en",
    )
    parser.add_argument(
        "--vad-filter",
        type=parse_bool,
        default=None,
        help="override daemon VAD filter setting",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=None,
        help="override daemon beam size",
    )
    parser.add_argument(
        "--best-of",
        type=int,
        default=None,
        help="override daemon best-of",
    )


def _add_output_args(parser):
    parser.add_argument(
        "--json",
        action="store_true",
        help="print full JSON response",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print diagnostics to stderr",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="list audio devices and exit",
    )
    parser.add_argument(
        "--recover-output-mute",
        action="store_true",
        help="attempt stale mute-state recovery and exit",
    )


def _add_mute_and_cancel_args(parser):
    parser.add_argument(
        "--mute-output",
        action=argparse.BooleanOptionalAction,
        default=parse_env_bool("KEYSTREL_MUTE_OUTPUT", True),
        help="mute all audio output sinks while recording",
    )
    parser.add_argument(
        "--mute-start-delay-ms",
        type=int,
        default=parse_env_int("KEYSTREL_MUTE_START_DELAY_MS", 0),
        help="delay muting output sinks after capture starts",
    )
    parser.add_argument(
        "--mute-settle-ms",
        type=int,
        default=parse_env_int("KEYSTREL_MUTE_SETTLE_MS", 300),
        help="max wait to confirm output mute before opening microphone stream",
    )
    parser.add_argument(
        "--cancel-file",
        default=get_env("KEYSTREL_CANCEL_FILE", ""),
        help="optional path used to cancel active capture",
    )


def _add_vad_args(parser):
    parser.add_argument(
        "--webrtcvad",
        action=argparse.BooleanOptionalAction,
        default=parse_env_bool("KEYSTREL_WEBRTCVAD", True),
        help="use WebRTC VAD to suppress steady background noise",
    )
    parser.add_argument(
        "--webrtcvad-mode",
        type=int,
        choices=(0, 1, 2, 3),
        default=parse_env_int("KEYSTREL_WEBRTCVAD_MODE", 2),
        help="WebRTC VAD aggressiveness (3 is most aggressive)",
    )
    parser.add_argument(
        "--webrtcvad-frame-ms",
        type=int,
        choices=(10, 20, 30),
        default=parse_env_int("KEYSTREL_WEBRTCVAD_FRAME_MS", 20),
        help="WebRTC VAD frame size in milliseconds",
    )
    parser.add_argument(
        "--speech-ratio",
        type=float,
        default=parse_env_float("KEYSTREL_SPEECH_RATIO", 0.60),
        help="minimum voiced-frame ratio in a block to count as speech",
    )
    parser.add_argument(
        "--start-speech-chunks",
        type=int,
        default=parse_env_int("KEYSTREL_START_SPEECH_CHUNKS", 2),
        help="consecutive speech-positive blocks required to start capture",
    )
    parser.add_argument(
        "--pre-roll-seconds",
        type=float,
        default=parse_env_float("KEYSTREL_PRE_ROLL_SECONDS", 0.35),
        help="seconds of audio to retain before speech start",
    )
    parser.add_argument(
        "--noise-multiplier",
        type=float,
        default=parse_env_float("KEYSTREL_NOISE_MULTIPLIER", 2.5),
        help="RMS fallback multiplier over measured noise floor",
    )


def _add_timeout_and_chime_args(parser):
    parser.add_argument(
        "--socket-timeout",
        type=float,
        default=parse_env_float("KEYSTREL_SOCKET_TIMEOUT", 20.0),
        help="seconds before daemon socket connect/read timeout",
    )
    parser.add_argument(
        "--start-chime",
        action=argparse.BooleanOptionalAction,
        default=parse_env_bool("KEYSTREL_START_CHIME", True),
        help="play an audible chime before muting output and listening",
    )
    parser.add_argument(
        "--chime-backend",
        choices=("auto", "pipewire", "paplay", "canberra", "sounddevice"),
        default=parse_env_choice(
            "KEYSTREL_CHIME_BACKEND",
            "pipewire",
            {"auto", "pipewire", "paplay", "canberra", "sounddevice"},
        ),
        help="chime playback backend preference",
    )
    parser.add_argument(
        "--chime-file",
        default=get_env("KEYSTREL_CHIME_FILE", "/usr/share/sounds/freedesktop/stereo/bell.oga"),
        help="audio file path for paplay/canberra chime backends",
    )
    parser.add_argument(
        "--chime-sink",
        default=get_env("KEYSTREL_CHIME_SINK", ""),
        help="optional paplay sink name/id (empty uses default sink)",
    )
    parser.add_argument(
        "--chime-target",
        default=get_env("KEYSTREL_CHIME_TARGET", ""),
        help="optional PipeWire target node serial/name (empty uses auto)",
    )
    parser.add_argument(
        "--chime-role",
        default=get_env("KEYSTREL_CHIME_ROLE", "Music"),
        help="PipeWire media role for chime stream (e.g. Music, Communication)",
    )
    parser.add_argument(
        "--chime-event-id",
        default=get_env("KEYSTREL_CHIME_EVENT_ID", "bell"),
        help="desktop sound event id for canberra backend",
    )
    parser.add_argument(
        "--chime-freq-hz",
        type=float,
        default=parse_env_float("KEYSTREL_CHIME_FREQ_HZ", 2400.0),
        help="start chime base frequency in Hz",
    )
    parser.add_argument(
        "--chime-duration-ms",
        type=int,
        default=parse_env_int("KEYSTREL_CHIME_DURATION_MS", 70),
        help="start chime duration in milliseconds",
    )
    parser.add_argument(
        "--chime-volume",
        type=float,
        default=parse_env_float("KEYSTREL_CHIME_VOLUME", 0.55),
        help="start chime output level (0.0-1.0)",
    )
    parser.add_argument(
        "--chime-cooldown-ms",
        type=int,
        default=parse_env_int("KEYSTREL_CHIME_COOLDOWN_MS", 20),
        help="wait after chime before muting/listening",
    )


def _normalize_args(args):
    args.sample_rate = max(1, args.sample_rate)
    args.max_seconds = max(0.1, args.max_seconds)
    args.min_seconds = max(0.0, args.min_seconds)
    if args.min_seconds > args.max_seconds:
        args.min_seconds = args.max_seconds
    args.silence_seconds = max(0.0, args.silence_seconds)
    args.block_seconds = max(0.01, args.block_seconds)
    args.threshold = max(0.0, args.threshold)
    args.mute_start_delay_ms = max(0, args.mute_start_delay_ms)
    args.mute_settle_ms = max(0, args.mute_settle_ms)
    args.speech_ratio = max(0.0, min(1.0, args.speech_ratio))
    args.start_speech_chunks = max(1, args.start_speech_chunks)
    args.pre_roll_seconds = max(0.0, args.pre_roll_seconds)
    args.noise_multiplier = max(1.0, args.noise_multiplier)
    args.socket_timeout = max(0.1, args.socket_timeout)
    args.server_timeout = max(0.1, args.server_timeout)
    args.server = args.server.strip()
    args.server_token = args.server_token.strip()
    args.cancel_file = str(Path(args.cancel_file).expanduser()).strip() if args.cancel_file else ""
    args.device = normalize_audio_device(args.device)
    args.chime_freq_hz = max(100.0, min(4000.0, args.chime_freq_hz))
    args.chime_duration_ms = max(20, args.chime_duration_ms)
    args.chime_volume = max(0.0, min(1.0, args.chime_volume))
    args.chime_cooldown_ms = max(0, args.chime_cooldown_ms)
    args.chime_file = str(Path(args.chime_file).expanduser())
    args.chime_sink = args.chime_sink.strip()
    args.chime_target = args.chime_target.strip()
    args.chime_role = args.chime_role.strip() or "Music"
    args.chime_event_id = args.chime_event_id.strip() or "bell"
    return args


def parse_args():
    parser = argparse.ArgumentParser(
        description="Record microphone audio and request transcription from keystrel-daemon"
    )
    _add_transport_args(parser)
    _add_capture_args(parser)
    _add_transcription_override_args(parser)
    _add_output_args(parser)
    _add_mute_and_cancel_args(parser)
    _add_vad_args(parser)
    _add_timeout_and_chime_args(parser)

    args = parser.parse_args()
    return _normalize_args(args)


def parse_server_endpoint(server_url):
    text = server_url.strip()
    if not text:
        return None

    if "://" not in text:
        text = f"tcp://{text}"

    parsed = urlparse(text)
    if parsed.scheme.lower() != "tcp":
        raise ValueError(f"unsupported KEYSTREL_SERVER scheme: {parsed.scheme or 'none'}")

    if parsed.path not in {"", "/"}:
        raise ValueError("KEYSTREL_SERVER must not include a path; use tcp://host:port")

    host = parsed.hostname
    if not host:
        raise ValueError("KEYSTREL_SERVER is missing host")

    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid KEYSTREL_SERVER port: {exc}") from exc

    port = parsed_port if parsed_port is not None else 8765
    if port <= 0 or port > 65535:
        raise ValueError(f"invalid KEYSTREL_SERVER port: {port}")

    return host, port


def build_transcription_options(args):
    payload = {}
    if args.language.strip():
        payload["language"] = args.language.strip()
    if args.vad_filter is not None:
        payload["vad_filter"] = args.vad_filter
    if args.beam_size is not None:
        payload["beam_size"] = args.beam_size
    if args.best_of is not None:
        payload["best_of"] = args.best_of
    return payload


def _default_pactl_timeout_s():
    global _PACTL_TIMEOUT_OVERRIDE_S
    if _PACTL_TIMEOUT_OVERRIDE_S is not None:
        return _PACTL_TIMEOUT_OVERRIDE_S

    configured = parse_env_float("KEYSTREL_PACTL_TIMEOUT_S", PACTL_CALL_TIMEOUT_S)
    _PACTL_TIMEOUT_OVERRIDE_S = max(0.05, configured)
    return _PACTL_TIMEOUT_OVERRIDE_S


def _run_pactl(command_args, timeout_s=None, retries=None):
    timeout_value = _default_pactl_timeout_s() if timeout_s is None else max(0.01, float(timeout_s))
    retry_count = PACTL_RETRY_COUNT if retries is None else max(0, int(retries))
    command_text = " ".join(str(part) for part in command_args)
    last_error = RuntimeError(f"pactl {command_text} failed")

    for attempt in range(retry_count + 1):
        try:
            result = subprocess.run(
                ["pactl", *command_args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_value,
            )
        except subprocess.TimeoutExpired:
            last_error = RuntimeError(
                f"pactl {command_text} timed out after {timeout_value:.2f}s"
            )
        except OSError as exc:
            last_error = RuntimeError(f"pactl {command_text} failed: {exc}")
        else:
            if result.returncode == 0:
                return result

            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            message = (
                stderr
                or stdout
                or f"pactl {command_text} failed with exit code {result.returncode}"
            )
            last_error = RuntimeError(message)

        if attempt >= retry_count:
            break

        backoff_s = PACTL_RETRY_BASE_DELAY_S * (2**attempt)
        jitter_s = random.uniform(0.0, PACTL_RETRY_JITTER_S)
        time.sleep(backoff_s + jitter_s)

    raise last_error


def list_output_sink_details(timeout_s=None, retries=None):
    result = _run_pactl(
        ["list", "short", "sinks"],
        timeout_s=timeout_s,
        retries=retries,
    )

    sinks = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 1:
            parts = line.split()
        if not parts:
            continue

        sink_id = parts[0].strip()
        if not sink_id:
            continue
        sink_name = parts[1].strip() if len(parts) > 1 else ""
        sinks.append({"sink": sink_id, "name": sink_name})
    return sinks


def list_output_sinks(timeout_s=None, retries=None):
    return [
        sink_info["sink"]
        for sink_info in list_output_sink_details(timeout_s=timeout_s, retries=retries)
    ]


def _coerce_sink_state_records(sink_states):
    records = {}
    if not isinstance(sink_states, dict):
        return records

    for raw_sink, raw_state in sink_states.items():
        sink = str(raw_sink).strip()
        if not sink:
            continue

        if isinstance(raw_state, dict):
            was_muted = bool(raw_state.get("was_muted", False))
            changed = bool(raw_state.get("changed", not was_muted))
            name = str(raw_state.get("name", "") or "").strip()
        else:
            was_muted = bool(raw_state)
            changed = not was_muted
            name = ""

        records[sink] = {
            "sink": sink,
            "name": name,
            "was_muted": was_muted,
            "changed": changed,
        }

    return records


def _changed_sink_states(sink_states):
    records = _coerce_sink_state_records(sink_states)
    return {sink: record for sink, record in records.items() if record["changed"]}


def _mute_transaction_path(args=None):
    explicit_path = ""
    if args is not None:
        explicit_path = str(getattr(args, "mute_transaction_file", "") or "").strip()
    if explicit_path:
        return Path(explicit_path).expanduser()

    default_path = "~/.cache/keystrel/keystrel-mute-transaction.json"
    configured_path = get_env("KEYSTREL_MUTE_TRANSACTION_FILE", default_path)
    return Path(configured_path or default_path).expanduser()


def _clear_mute_transaction(args):
    path = _mute_transaction_path(args)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_mute_transaction(args, sink_states, reason):
    changed_states = _changed_sink_states(sink_states)
    if not changed_states:
        _clear_mute_transaction(args)
        return

    path = _mute_transaction_path(args)
    payload = {
        "version": MUTE_TRANSACTION_VERSION,
        "reason": reason,
        "pid": os.getpid(),
        "updated_unix_s": time.time(),
        "sinks": list(changed_states.values()),
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(encoded, encoding="utf-8")
        temp_path.replace(path)
    except OSError as exc:
        if getattr(args, "verbose", False):
            print(
                f"[keystrel-client] failed to persist mute transaction {path}: {exc}",
                file=sys.stderr,
            )


def _load_mute_transaction(args):
    path = _mute_transaction_path(args)
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(
            f"[keystrel-client] WARNING: could not parse mute transaction file {path}: {exc}",
            file=sys.stderr,
        )
        _clear_mute_transaction(args)
        return {}

    if not isinstance(payload, dict):
        _clear_mute_transaction(args)
        return {}

    sinks = payload.get("sinks", [])
    if not isinstance(sinks, list):
        _clear_mute_transaction(args)
        return {}

    restored = {}
    for item in sinks:
        if not isinstance(item, dict):
            continue
        sink = str(item.get("sink", "") or "").strip()
        if not sink:
            continue
        restored[sink] = {
            "sink": sink,
            "name": str(item.get("name", "") or "").strip(),
            "was_muted": bool(item.get("was_muted", False)),
            "changed": bool(item.get("changed", True)),
        }

    if not restored:
        _clear_mute_transaction(args)
    return restored


def _stable_sink_identifier(record):
    sink = str(record.get("sink", "")).strip()
    name = str(record.get("name", "")).strip()
    if name:
        return f"{sink}({name})"
    return sink


def get_sink_mute_state(sink, timeout_s=None, retries=None):
    result = _run_pactl(
        ["get-sink-mute", sink],
        timeout_s=timeout_s,
        retries=retries,
    )

    output = result.stdout.strip().lower()
    if output.endswith("yes"):
        return True
    if output.endswith("no"):
        return False
    raise RuntimeError(f"unexpected pactl output for sink {sink}: {result.stdout.strip()}")


def set_sink_mute_state(sink, muted, timeout_s=None, retries=None):
    _run_pactl(
        ["set-sink-mute", sink, "1" if muted else "0"],
        timeout_s=timeout_s,
        retries=retries,
    )


def mute_output_during_capture(args):
    if not args.mute_output:
        return {}

    if not shutil.which("pactl"):
        if args.verbose:
            print(
                "[keystrel-client] --mute-output requested but pactl is not installed",
                file=sys.stderr,
            )
        return {}

    sink_states = {}
    try:
        sinks = list_output_sinks()
        sink_names = {}
        try:
            sink_names = {
                sink_info["sink"]: sink_info.get("name", "")
                for sink_info in list_output_sink_details(timeout_s=_default_pactl_timeout_s(), retries=0)
            }
        except Exception:  # noqa: BLE001
            sink_names = {}

        for sink in sinks:
            sink_name = sink_names.get(sink, "")
            was_muted = get_sink_mute_state(sink)
            changed = not was_muted
            sink_states[sink] = {
                "sink": sink,
                "name": sink_name,
                "was_muted": was_muted,
                "changed": changed,
            }
            if changed:
                set_sink_mute_state(sink, True)
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] output mute setup failed: {exc}", file=sys.stderr)
        return sink_states

    if args.verbose and sink_states:
        changed_count = len(_changed_sink_states(sink_states))
        print(
            f"[keystrel-client] muted {changed_count} output sink(s) "
            f"(observed {len(sink_states)} total)",
            file=sys.stderr,
        )
    return sink_states


def _resolve_restore_candidates(record, live_sink_by_name, live_name_by_sink):
    original_sink = record["sink"]
    record_name = record.get("name", "")
    remapped_sink = live_sink_by_name.get(record_name)
    original_live_name = live_name_by_sink.get(original_sink, "")

    candidates = []
    if record.get("changed") and record_name:
        if remapped_sink:
            candidates.append(remapped_sink)
        if not original_live_name or original_live_name == record_name:
            candidates.append(original_sink)
    else:
        candidates.append(original_sink)

    deduped = []
    seen = set()
    for candidate in candidates:
        text = str(candidate).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def restore_output_mute(args, sink_states):
    records = _coerce_sink_state_records(sink_states)
    if not records:
        return {}

    live_sink_by_name = {}
    live_name_by_sink = {}
    try:
        for sink_info in list_output_sink_details(timeout_s=_default_pactl_timeout_s(), retries=0):
            sink = str(sink_info.get("sink", "") or "").strip()
            name = str(sink_info.get("name", "") or "").strip()
            if not sink:
                continue
            live_name_by_sink[sink] = name
            if name:
                live_sink_by_name[name] = sink
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] restore sink remap unavailable: {exc}", file=sys.stderr)

    unresolved = {}
    ordered_records = sorted(
        records.values(),
        key=lambda record: (0 if record["changed"] else 1, record["sink"]),
    )
    for record in ordered_records:
        attempts = []
        restored = False
        for candidate in _resolve_restore_candidates(record, live_sink_by_name, live_name_by_sink):
            try:
                set_sink_mute_state(candidate, record["was_muted"])
                if candidate != record["sink"] and args.verbose:
                    print(
                        "[keystrel-client] restore remapped sink "
                        f"{record['sink']} -> {candidate} ({record['name']})",
                        file=sys.stderr,
                    )
                restored = True
                break
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{candidate}: {exc}")

        if not restored:
            unresolved[record["sink"]] = {
                "sink": record["sink"],
                "name": record["name"],
                "was_muted": record["was_muted"],
                "changed": record["changed"],
                "attempts": attempts,
            }

    if unresolved:
        failed_sinks = ", ".join(
            _stable_sink_identifier(record)
            for _, record in sorted(unresolved.items(), key=lambda item: item[0])
        )
        print(
            "[keystrel-client] WARNING: failed to restore mute state for "
            f"{len(unresolved)}/{len(records)} sink(s): {failed_sinks}; "
            "run `keystrel-unmute` for manual recovery",
            file=sys.stderr,
        )
        if args.verbose:
            for sink, record in sorted(unresolved.items()):
                attempt_text = "; ".join(record.get("attempts", [])) or "no attempts"
                print(
                    f"[keystrel-client] restore detail sink={sink}: {attempt_text}",
                    file=sys.stderr,
                )

    if args.verbose:
        restored = len(records) - len(unresolved)
        print(
            f"[keystrel-client] restored mute state for {restored}/{len(records)} sink(s)",
            file=sys.stderr,
        )

    return {
        sink: {
            "sink": record["sink"],
            "name": record["name"],
            "was_muted": record["was_muted"],
            "changed": record["changed"],
        }
        for sink, record in unresolved.items()
    }


def cancel_requested(args):
    cancel_file = str(getattr(args, "cancel_file", "") or "").strip()
    if not cancel_file:
        return False
    try:
        return Path(cancel_file).exists()
    except OSError:
        return False


def confirm_output_mute_before_capture(args, sink_states):
    if args.mute_settle_ms <= 0 or not sink_states:
        return

    pending = {
        sink
        for sink, record in _coerce_sink_state_records(sink_states).items()
        if record["changed"]
    }
    if not pending:
        return

    timeout_s = args.mute_settle_ms / 1000.0
    deadline = time.monotonic() + timeout_s

    if args.verbose:
        print(
            f"[keystrel-client] confirming mute on {len(pending)} sink(s) "
            f"for up to {args.mute_settle_ms}ms before mic start",
            file=sys.stderr,
        )

    while pending:
        if cancel_requested(args):
            raise CaptureCancelled()

        still_pending = set()
        pending_order = sorted(pending)
        for index, sink in enumerate(pending_order):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                still_pending.update(pending_order[index:])
                break
            per_call_timeout_s = max(0.01, min(_default_pactl_timeout_s(), remaining))
            try:
                if not get_sink_mute_state(sink, timeout_s=per_call_timeout_s, retries=0):
                    still_pending.add(sink)
            except Exception:  # noqa: BLE001
                still_pending.add(sink)
        pending = still_pending

        if not pending:
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.02, remaining))

    if args.verbose:
        pending_sinks = ", ".join(sorted(pending))
        print(
            "[keystrel-client] mute confirmation timed out; "
            f"starting mic with unconfirmed sink(s): {pending_sinks}",
            file=sys.stderr,
        )


def _warmup_output_control(args):
    if not getattr(args, "mute_output", False):
        return
    if not shutil.which("pactl"):
        return

    try:
        _run_pactl(["info"], timeout_s=_default_pactl_timeout_s(), retries=0)
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] output control warm-up failed: {exc}", file=sys.stderr)


def _finalize_output_mute_cleanup(args, sink_states, state, reason):
    if state.get("done") or state.get("in_progress"):
        return

    records = _coerce_sink_state_records(sink_states)
    if not records:
        state["done"] = True
        return

    state["in_progress"] = True
    try:
        unresolved = restore_output_mute(args, records)
        if unresolved:
            _write_mute_transaction(args, unresolved, f"{reason}-restore-failed")
        else:
            _clear_mute_transaction(args)
        state["done"] = True
    finally:
        state["in_progress"] = False


def _install_output_restore_signal_handlers(args, sink_states, cleanup_state):
    previous_handlers = {}

    def handle_termination(signum, _frame):
        signal_name = getattr(signal.Signals(signum), "name", str(signum))
        if args.verbose:
            print(
                f"[keystrel-client] received {signal_name}; restoring output mute state",
                file=sys.stderr,
            )
        _finalize_output_mute_cleanup(args, sink_states, cleanup_state, f"signal-{signal_name}")
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_termination)
        except Exception:  # noqa: BLE001
            continue

    return previous_handlers


def _restore_previous_signal_handlers(previous_handlers):
    for signum, previous_handler in previous_handlers.items():
        try:
            signal.signal(signum, previous_handler)
        except Exception:  # noqa: BLE001
            continue


def recover_stale_output_mute(args, force=False):
    sink_states = _load_mute_transaction(args)
    if not sink_states:
        return True

    if not shutil.which("pactl"):
        print(
            "[keystrel-client] WARNING: stale mute-state recovery skipped because pactl is unavailable",
            file=sys.stderr,
        )
        return False

    if args.verbose or force:
        print("[keystrel-client] recovering stale output mute state", file=sys.stderr)

    unresolved = restore_output_mute(args, sink_states)
    if unresolved:
        _write_mute_transaction(args, unresolved, "startup-recovery-failed")
        return False

    _clear_mute_transaction(args)
    if args.verbose or force:
        print("[keystrel-client] stale output mute recovery completed", file=sys.stderr)
    return True


def acquire_client_lock(args):
    lock_value = get_env("KEYSTREL_CLIENT_LOCK", "~/.cache/keystrel/keystrel-client.lock")
    lock_path = Path(lock_value or "~/.cache/keystrel/keystrel-client.lock").expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        if args.verbose:
            print("[keystrel-client] another capture is already running", file=sys.stderr)
        return None
    return lock_file


def build_webrtc_vad(args):
    if not args.webrtcvad:
        return None

    if webrtcvad is None:
        if args.verbose:
            print("[keystrel-client] WebRTC VAD unavailable, using RMS fallback", file=sys.stderr)
        return None
    if args.sample_rate not in WEBRTCVAD_SAMPLE_RATES:
        if args.verbose:
            print(
                f"[keystrel-client] sample rate {args.sample_rate} unsupported for WebRTC VAD; "
                "using RMS fallback",
                file=sys.stderr,
            )
        return None
    if args.webrtcvad_frame_ms not in WEBRTCVAD_FRAME_MS:
        if args.verbose:
            print(
                f"[keystrel-client] frame size {args.webrtcvad_frame_ms}ms unsupported for WebRTC VAD; "
                "using RMS fallback",
                file=sys.stderr,
            )
        return None

    try:
        return webrtcvad.Vad(args.webrtcvad_mode)
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] failed to initialize WebRTC VAD: {exc}", file=sys.stderr)
        return None


def _query_devices_with_default_input_index():
    try:
        devices = sd.query_devices()
        default_device = sd.default.device
        try:
            default_input_index = int(default_device[0])
        except Exception:  # noqa: BLE001
            default_input_index = int(default_device)
    except Exception:  # noqa: BLE001
        return None, None

    return devices, default_input_index


def _default_input_looks_virtual(devices, default_input_index):
    if default_input_index < 0 or default_input_index >= len(devices):
        return False

    default_info = devices[default_input_index]
    default_name = str(default_info.get("name", "")).strip().lower()
    default_inputs = int(default_info.get("max_input_channels", 0) or 0)

    return default_name in {"default", "pipewire"} or default_inputs >= 16


def _build_input_candidate(index, info, args):
    name = str(info.get("name", "")).strip()
    lowered = name.lower()
    inputs = int(info.get("max_input_channels", 0) or 0)
    outputs = int(info.get("max_output_channels", 0) or 0)

    if inputs <= 0:
        return None
    if outputs != 0:
        return None
    if lowered in {"default", "pipewire"}:
        return None
    if "monitor" in lowered:
        return None

    try:
        sd.check_input_settings(
            device=index,
            channels=args.channels,
            samplerate=args.sample_rate,
            dtype="float32",
        )
    except Exception:  # noqa: BLE001
        return None

    score = 0
    if "usb" in lowered:
        score += 3
    if "mic" in lowered or "microphone" in lowered:
        score += 3
    if "mono" in lowered:
        score += 1

    return score, -index, index, name


def auto_select_input_device(args):
    if args.device is not None:
        return args.device, False

    devices, default_input_index = _query_devices_with_default_input_index()
    if devices is None or default_input_index is None:
        return None, False

    if not _default_input_looks_virtual(devices, default_input_index):
        return None, False

    candidates = []
    for index, info in enumerate(devices):
        candidate = _build_input_candidate(index, info, args)
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        return None, False

    candidates.sort(reverse=True)
    _, _, selected_index, selected_name = candidates[0]

    if args.verbose:
        print(
            "[keystrel-client] default input appears virtual; "
            f"auto-selecting capture device {selected_index}: {selected_name}",
            file=sys.stderr,
        )
    return selected_index, True


def speech_ratio_in_chunk(chunk, args, vad):
    if vad is None:
        return None

    mono = chunk
    if mono.ndim > 1:
        mono = np.mean(mono, axis=1)
    mono = np.clip(mono, -1.0, 1.0)
    pcm16 = (mono * 32767.0).astype(np.int16)

    frame_samples = int(args.sample_rate * args.webrtcvad_frame_ms / 1000)
    if frame_samples <= 0:
        return None

    total_frames = 0
    voiced_frames = 0
    end = len(pcm16) - frame_samples + 1
    if end <= 0:
        return 0.0

    for start in range(0, end, frame_samples):
        frame = pcm16[start : start + frame_samples]
        if len(frame) != frame_samples:
            continue
        total_frames += 1
        try:
            if vad.is_speech(frame.tobytes(), args.sample_rate):
                voiced_frames += 1
        except Exception:  # noqa: BLE001
            return None

    if total_frames == 0:
        return 0.0
    return voiced_frames / total_frames


def _detect_voice_activity(chunk, args, vad, started_voice, noise_floor):
    rms = float(np.sqrt(np.mean(np.square(chunk))))
    speech_ratio = speech_ratio_in_chunk(chunk, args, vad)
    if speech_ratio is not None:
        return speech_ratio >= args.speech_ratio, noise_floor

    if not started_voice:
        if noise_floor is None:
            noise_floor = rms
        else:
            noise_floor = 0.9 * noise_floor + 0.1 * rms

    dynamic_threshold = args.threshold
    if noise_floor is not None:
        dynamic_threshold = max(dynamic_threshold, noise_floor * args.noise_multiplier)
    return rms >= dynamic_threshold, noise_floor


def _should_stop_after_silence(args, started_voice, elapsed, last_voice_at, now):
    return (
        started_voice
        and elapsed >= args.min_seconds
        and last_voice_at is not None
        and (now - last_voice_at) >= args.silence_seconds
    )


def _build_stream_kwargs(args, blocksize, callback):
    stream_kwargs = {
        "samplerate": args.sample_rate,
        "channels": args.channels,
        "dtype": "float32",
        "blocksize": blocksize,
        "callback": callback,
    }
    capture_device, _ = auto_select_input_device(args)
    if capture_device is not None:
        stream_kwargs["device"] = capture_device
    return stream_kwargs


def _update_capture_state(
    chunk,
    args,
    now,
    is_voice,
    started_voice,
    speech_streak,
    pre_roll_chunks,
    chunks,
    last_voice_at,
):
    if is_voice:
        speech_streak += 1
    else:
        speech_streak = 0

    if not started_voice:
        if pre_roll_chunks is not None:
            pre_roll_chunks.append(chunk)
        if speech_streak >= args.start_speech_chunks:
            started_voice = True
            last_voice_at = now
            if pre_roll_chunks is not None:
                chunks.extend(pre_roll_chunks)
                pre_roll_chunks.clear()
        return started_voice, speech_streak, last_voice_at

    chunks.append(chunk)
    if is_voice:
        last_voice_at = now

    return started_voice, speech_streak, last_voice_at


def _log_capture_config(args, vad):
    if not args.verbose:
        return

    print(
        "[keystrel-client] recording "
        f"max={args.max_seconds}s min={args.min_seconds}s silence={args.silence_seconds}s "
        f"threshold={args.threshold} webrtcvad={'on' if vad is not None else 'off'}",
        file=sys.stderr,
    )


def _call_capture_tick(on_tick, elapsed):
    if on_tick is None:
        return

    try:
        on_tick(elapsed)
    except CaptureCancelled:
        raise
    except Exception:  # noqa: BLE001
        pass


def _compute_capture_queue_timeout(max_seconds, elapsed, poll_timeout_s):
    if elapsed >= max_seconds:
        return None

    remaining_s = max_seconds - elapsed
    if remaining_s <= 0:
        return None

    return max(0.001, min(poll_timeout_s, remaining_s))


def _read_capture_chunk(audio_queue, queue_timeout_s):
    try:
        return audio_queue.get(timeout=queue_timeout_s)
    except queue.Empty:
        return None


def record_until_silence(args, on_tick=None):
    blocksize = max(1, int(args.sample_rate * args.block_seconds))
    poll_timeout_s = min(0.05, max(0.01, float(args.block_seconds)))
    audio_queue = queue.Queue()
    chunks = []
    pre_roll_chunk_count = int(args.pre_roll_seconds / args.block_seconds)
    pre_roll_chunks = deque(maxlen=pre_roll_chunk_count) if pre_roll_chunk_count > 0 else None
    started_voice = False
    started_at = time.monotonic()
    last_voice_at = None
    speech_streak = 0
    noise_floor = None
    vad = build_webrtc_vad(args)

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        if status and args.verbose:
            print(f"[keystrel-client] audio status: {status}", file=sys.stderr)
        audio_queue.put(indata.copy())

    stream_kwargs = _build_stream_kwargs(args, blocksize, callback)
    _log_capture_config(args, vad)

    with sd.InputStream(**stream_kwargs):
        while True:
            if cancel_requested(args):
                raise CaptureCancelled()

            now = time.monotonic()
            elapsed = now - started_at
            _call_capture_tick(on_tick, elapsed)

            queue_timeout_s = _compute_capture_queue_timeout(args.max_seconds, elapsed, poll_timeout_s)
            if queue_timeout_s is None:
                break

            chunk = _read_capture_chunk(audio_queue, queue_timeout_s)
            if chunk is None:
                continue

            is_voice, noise_floor = _detect_voice_activity(
                chunk,
                args,
                vad,
                started_voice,
                noise_floor,
            )
            started_voice, speech_streak, last_voice_at = _update_capture_state(
                chunk,
                args,
                now,
                is_voice,
                started_voice,
                speech_streak,
                pre_roll_chunks,
                chunks,
                last_voice_at,
            )

            if not started_voice:
                continue

            if _should_stop_after_silence(args, started_voice, elapsed, last_voice_at, now):
                break

    if not chunks or not started_voice:
        return np.empty((0, args.channels), dtype=np.float32)
    return np.concatenate(chunks, axis=0)


def _play_chime_paplay(args):
    if not shutil.which("paplay"):
        return False

    chime_file = Path(args.chime_file)
    if not chime_file.is_file():
        return False

    cmd = ["paplay"]
    if args.chime_sink:
        cmd.extend(["--device", args.chime_sink])
    cmd.append(str(chime_file))

    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] paplay chime failed: {exc}", file=sys.stderr)
        return False

    if result.returncode == 0:
        if args.verbose:
            sink_desc = args.chime_sink if args.chime_sink else "default-sink"
            print(f"[keystrel-client] start chime played (paplay:{sink_desc})", file=sys.stderr)
        return True

    if args.verbose:
        stderr = result.stderr.strip()
        print(
            f"[keystrel-client] paplay chime failed rc={result.returncode} {stderr}",
            file=sys.stderr,
        )
    return False


def _play_chime_pipewire(args):
    if not shutil.which("pw-play"):
        return False

    chime_file = Path(args.chime_file)
    if not chime_file.is_file():
        return False

    cmd = [
        "pw-play",
        "--media-role",
        args.chime_role,
        "--volume",
        str(args.chime_volume),
        "--latency",
        "20ms",
    ]
    if args.chime_target:
        cmd.extend(["--target", args.chime_target])
    cmd.append(str(chime_file))

    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] pipewire chime failed: {exc}", file=sys.stderr)
        return False

    if result.returncode == 0:
        if args.verbose:
            target_desc = args.chime_target if args.chime_target else "auto-target"
            print(f"[keystrel-client] start chime played (pipewire:{target_desc})", file=sys.stderr)
        return True

    if args.verbose:
        stderr = result.stderr.strip()
        print(
            f"[keystrel-client] pipewire chime failed rc={result.returncode} {stderr}",
            file=sys.stderr,
        )
    return False


def _play_chime_canberra(args):
    if not shutil.which("canberra-gtk-play"):
        return False

    cmd = ["canberra-gtk-play", "-d", "keystrel start"]
    if Path(args.chime_file).is_file():
        cmd.extend(["-f", args.chime_file])
    else:
        cmd.extend(["-i", args.chime_event_id])

    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] canberra chime failed: {exc}", file=sys.stderr)
        return False

    if result.returncode == 0:
        if args.verbose:
            print("[keystrel-client] start chime played (canberra)", file=sys.stderr)
        return True

    if args.verbose:
        stderr = result.stderr.strip()
        print(
            f"[keystrel-client] canberra chime failed rc={result.returncode} {stderr}",
            file=sys.stderr,
        )
    return False


def _play_chime_sounddevice(args):
    sample_rate = 48000
    duration_s = args.chime_duration_ms / 1000.0
    frame_count = max(1, int(sample_rate * duration_s))
    t = np.arange(frame_count, dtype=np.float32) / sample_rate

    start_hz = args.chime_freq_hz
    end_hz = min(5200.0, start_hz * 1.65)
    sweep_rate = (end_hz - start_hz) / max(duration_s, 1e-6)
    phase = 2.0 * np.pi * (start_hz * t + 0.5 * sweep_rate * t * t)

    waveform = (0.85 * np.sin(phase)) + (0.25 * np.sin(2.0 * phase))

    attack_samples = min(int(sample_rate * 0.002), frame_count)
    release_samples = min(int(sample_rate * 0.008), frame_count)
    if attack_samples > 0:
        attack = np.linspace(0.0, 1.0, attack_samples, dtype=np.float32)
        waveform[:attack_samples] *= attack
    if release_samples > 0:
        release = np.linspace(1.0, 0.0, release_samples, dtype=np.float32)
        waveform[-release_samples:] *= release

    waveform = (waveform * args.chime_volume).astype(np.float32)

    try:
        sd.play(waveform, samplerate=sample_rate, blocking=True)
        if args.verbose:
            print("[keystrel-client] start chime played (sounddevice)", file=sys.stderr)
        return True
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            print(f"[keystrel-client] sounddevice chime failed: {exc}", file=sys.stderr)
        return False
    finally:
        try:
            sd.stop()
        except Exception:  # noqa: BLE001
            pass


def play_start_chime(args):
    if not args.start_chime:
        return

    backend = args.chime_backend
    if backend == "auto":
        backend_order = ("pipewire", "paplay", "sounddevice", "canberra")
    elif backend == "pipewire":
        backend_order = ("pipewire", "paplay", "sounddevice", "canberra")
    elif backend == "sounddevice":
        backend_order = ("sounddevice", "pipewire", "paplay", "canberra")
    elif backend == "paplay":
        backend_order = ("paplay", "pipewire", "sounddevice", "canberra")
    else:
        backend_order = ("canberra", "pipewire", "paplay", "sounddevice")

    played = False
    for candidate in backend_order:
        if candidate == "sounddevice":
            played = _play_chime_sounddevice(args)
        elif candidate == "pipewire":
            played = _play_chime_pipewire(args)
        elif candidate == "paplay":
            played = _play_chime_paplay(args)
        elif candidate == "canberra":
            played = _play_chime_canberra(args)

        if played:
            break

    if not played and args.verbose:
        print("[keystrel-client] no chime backend succeeded", file=sys.stderr)

    if args.chime_cooldown_ms > 0:
        time.sleep(args.chime_cooldown_ms / 1000.0)


def send_unix_request(socket_path, payload, timeout_s):
    data = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.connect(str(socket_path))
            sock.sendall(data)
            response = b""
            while not response.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        except socket.timeout as exc:
            raise TimeoutError(f"daemon request timed out after {timeout_s:.1f}s") from exc

    if not response:
        raise RuntimeError("empty response from daemon")
    return json.loads(response.decode("utf-8"))


def send_tcp_request(host, port, payload, timeout_s, max_response_bytes=2 * 1024 * 1024):
    data = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")

    try:
        sock = socket.create_connection((host, port), timeout=timeout_s)
    except socket.timeout as exc:
        raise TimeoutError(f"remote server connect timed out after {timeout_s:.1f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"remote server connection failed: {exc}") from exc

    with sock:
        sock.settimeout(timeout_s)
        try:
            sock.sendall(data)
            response = b""
            while not response.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if len(response) > max_response_bytes:
                    raise RuntimeError("remote response exceeded size limit")
        except socket.timeout as exc:
            raise TimeoutError(
                f"remote server request timed out after {timeout_s:.1f}s"
            ) from exc

    if not response:
        raise RuntimeError("empty response from remote server")

    try:
        return json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON response from remote server: {exc}") from exc


def _resolve_transcription_target(args):
    try:
        remote_endpoint = parse_server_endpoint(args.server)
    except ValueError as exc:
        print(f"[keystrel-client] invalid remote server configuration: {exc}", file=sys.stderr)
        sys.exit(2)

    socket_path = None
    if remote_endpoint is None:
        socket_path = Path(args.socket).expanduser()
        if not socket_path.exists():
            print(
                f"[keystrel-client] daemon socket not found: {socket_path}\n"
                "[keystrel-client] start it with: systemctl --user start keystrel-daemon",
                file=sys.stderr,
            )
            sys.exit(2)
    elif not args.server_token:
        print(
            "[keystrel-client] remote mode requires KEYSTREL_SERVER_TOKEN or --server-token",
            file=sys.stderr,
        )
        sys.exit(2)

    return remote_endpoint, socket_path


def _capture_audio_with_output_control(args):
    sink_states = {}
    mute_applied = False
    cleanup_state = {"done": False, "in_progress": False}
    previous_signal_handlers = _install_output_restore_signal_handlers(
        args,
        sink_states,
        cleanup_state,
    )

    mute_start_delay_s = args.mute_start_delay_ms / 1000.0

    def apply_output_mute(reason):
        applied_states = mute_output_during_capture(args)
        sink_states.clear()
        sink_states.update(applied_states)
        _write_mute_transaction(args, sink_states, reason)

    def maybe_apply_mute(elapsed_s):
        nonlocal mute_applied
        if cancel_requested(args):
            raise CaptureCancelled()
        if mute_applied or not args.mute_output:
            return
        if elapsed_s < mute_start_delay_s:
            return
        apply_output_mute("capture-delayed-mute")
        mute_applied = True

    try:
        _warmup_output_control(args)
        play_start_chime(args)
        if cancel_requested(args):
            raise CaptureCancelled()
        if args.mute_output and mute_start_delay_s <= 0:
            apply_output_mute("capture-pre-roll-mute")
            mute_applied = True
            confirm_output_mute_before_capture(args, sink_states)
        return record_until_silence(args, on_tick=maybe_apply_mute)
    except CaptureCancelled:
        if args.verbose:
            print("[keystrel-client] capture cancelled", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[keystrel-client] microphone capture failed: {exc}", file=sys.stderr)
        sys.exit(3)
    finally:
        _restore_previous_signal_handlers(previous_signal_handlers)
        _finalize_output_mute_cleanup(args, sink_states, cleanup_state, "capture-finally")


def _should_skip_request(args, audio):
    if cancel_requested(args):
        if args.verbose:
            print("[keystrel-client] request skipped due cancel", file=sys.stderr)
        print("", end="")
        return True

    if audio is None or getattr(audio, "size", 0) == 0:
        print("", end="")
        return True

    return False


def _request_transcription(args, audio, remote_endpoint, socket_path):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    try:
        sf.write(str(wav_path), audio, args.sample_rate)
        payload = build_transcription_options(args)

        if remote_endpoint is None:
            payload["audio_path"] = str(wav_path)
            return send_unix_request(socket_path, payload, args.socket_timeout)

        host, port = remote_endpoint
        payload["audio_b64"] = base64.b64encode(wav_path.read_bytes()).decode("ascii")
        payload["auth_token"] = args.server_token
        return send_tcp_request(host, port, payload, args.server_timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"[keystrel-client] request failed: {exc}", file=sys.stderr)
        sys.exit(4)
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass


def _print_response(args, response):
    if not response.get("ok"):
        print(f"[keystrel-client] daemon error: {response.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(5)

    if args.verbose:
        print(
            f"[keystrel-client] elapsed={response.get('elapsed_s')}s language={response.get('language')}",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps(response, ensure_ascii=True))
    else:
        print(response.get("text", ""))


def main():
    args = parse_args()
    recover_only = bool(getattr(args, "recover_output_mute", False))

    if args.list_devices:
        print(sd.query_devices())
        return

    lock_file = acquire_client_lock(args)
    if lock_file is None:
        if recover_only:
            sys.exit(6)
        return

    try:
        recovered = recover_stale_output_mute(args, force=recover_only)
        if recover_only:
            if not recovered:
                sys.exit(6)
            return

        remote_endpoint, socket_path = _resolve_transcription_target(args)
        audio = _capture_audio_with_output_control(args)
        if _should_skip_request(args, audio):
            return
        response = _request_transcription(args, audio, remote_endpoint, socket_path)
        _print_response(args, response)
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
