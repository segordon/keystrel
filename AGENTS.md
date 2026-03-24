# STT Project Agent Guide

This file documents what has been built so far for the Ubuntu GPU speech-to-text workflow and how future agents should work on it safely.

## Project Goal

Provide fast, local, GPU-accelerated speech-to-text (STT) for terminal workflows (including interactive prompt tools), with practical push-to-talk behavior and no dependency on piping stdin into the app.

## Current Environment

- OS/session: Ubuntu derivative, GNOME, X11 session.
- GPU: NVIDIA RTX 5070 Ti with working NVIDIA driver/CUDA runtime.
- Python: 3.12.
- STT backend: `faster-whisper` with CUDA via `ctranslate2`.

## What Was Implemented

### 1) GPU backend install and validation

- Created venv at `/home/user/.venvs/faster-whisper`.
- Installed core Python packages:
  - `faster-whisper`
  - `nvidia-cublas-cu12`
  - `nvidia-cudnn-cu12`
  - `sounddevice`
  - `soundfile`
- Added env helper: `/home/user/.venvs/faster-whisper/env.sh`
  - exports `VIRTUAL_ENV`
  - prepends `PATH`
  - sets `LD_LIBRARY_PATH` for cuBLAS/cuDNN wheel libs
- Verified:
  - CUDA visible through `ctranslate2`
  - model load on GPU succeeds

### 2) Warm daemon architecture

- Added daemon script: `/home/user/.local/lib/stt/stt_daemon.py`
- Daemon runs a persistent Whisper model and serves requests over Unix socket.
- Socket default: `/home/user/.cache/stt/faster-whisper.sock`
- Protocol: single-line JSON request/response over AF_UNIX stream.

Request (minimum):

```json
{"audio_path":"/tmp/file.wav"}
```

Optional request fields supported:

- `language`
- `task`
- `vad_filter`
- `beam_size`
- `best_of`

Response contains:

- `ok`
- `text`
- `language`
- `language_probability`
- `elapsed_s`

### 3) Client capture tool

- Added client script: `/home/user/.local/lib/stt/stt_client.py`
- Captures mic audio with `sounddevice`, VAD-like stop logic based on RMS threshold + trailing silence.
- Writes temp WAV, sends to daemon, prints transcript to stdout.

Key behavior:

- Returns empty output if voice was never detected (`started_voice` guard).
- Supports overrides for language/vad/beam/best_of.
- Supports device listing via `--list-devices`.
- Uses WebRTC VAD gating (when available) to reduce fan/noise false triggers.
- Requires consecutive speech-positive blocks before capture start.
- Keeps short pre-roll audio so initial words are not clipped.
- Falls back to adaptive RMS thresholding if WebRTC VAD is unavailable.

### 4) Audio-out mute while listening

- Implemented in client with `pactl` sink control.
- During capture:
  - enumerate sinks
  - snapshot mute state per sink
  - mute all currently unmuted sinks
- After capture (always, `finally`):
  - restore each sink to original state
- Added CLI switch:
  - `--mute-output` / `--no-mute-output`
- Default mute behavior currently enabled via env default logic (`STT_MUTE_OUTPUT` falls back to `1`).

### 5) Concurrency/race protections

Two separate protections were added to fix real issues seen in testing.

1. Client lock (`stt_client.py`)
   - Non-blocking file lock at `~/.cache/stt/stt-client.lock`.
   - If a capture is already active, new run exits quietly.
   - Prevents overlapping captures and sink-state races.

2. PTT lock + debounce (`stt-ptt`)
   - Lock file: `${XDG_RUNTIME_DIR:-$HOME/.cache/stt}/stt-ptt.lock`
   - Debounce stamp: `${XDG_RUNTIME_DIR:-$HOME/.cache/stt}/stt-ptt.last`
   - Default debounce: `0ms` via `STT_PTT_DEBOUNCE_MS` (lock handles overlap protection)
   - Prevents key-repeat spawning multiple jobs and delayed/batched output typing.

### 6) PTT integration for GNOME Terminal

- Installed `xdotool` and created launcher: `/home/user/.local/bin/stt-ptt`
- Flow:
  1. run `stt-client`
  2. sanitize transcript (CR/LF)
  3. type into focused window with `xdotool type`
  4. optional Enter key if `STT_PTT_SEND_ENTER=1`

PTT script safety checks:

- requires X11 session
- requires executable `stt-client`
- requires `xdotool`
- requires `flock`

### 7) Systemd user service

- Service file: `/home/user/.config/systemd/user/stt-daemon.service`
- Env file: `/home/user/.config/stt-daemon.env`
- Wrapper: `/home/user/.local/bin/stt-daemon`
- Service is enabled and started (`systemctl --user`).

### 8) Additional noise suppression tuning

Client defaults were hardened against steady ambient noise (fan/hum):

- `STT_WEBRTCVAD=1`
- `STT_WEBRTCVAD_MODE=2`
- `STT_WEBRTCVAD_FRAME_MS=20`
- `STT_SPEECH_RATIO=0.60`
- `STT_START_SPEECH_CHUNKS=2`
- `STT_PRE_ROLL_SECONDS=0.35`

These settings reduce false speech starts while still keeping spoken onset via pre-roll.

### 9) Runtime hardening pass

Additional robustness/security improvements were applied:

- Daemon now refuses to unlink non-socket paths when preparing the socket path.
- Daemon socket directory/file permissions are tightened (`0700` dir under home, `0600` socket file).
- Client now supports daemon socket timeout (`--socket-timeout`, env `STT_SOCKET_TIMEOUT`).
- Client/daemon env parsing now tolerates malformed env values and falls back to defaults (with warnings) instead of traceback crashes.
- Wrapper scripts were de-hardcoded to use `$HOME` and overridable env paths.
- PTT debounce/type delay env values are validated before use.

### 10) Audible start chime before listen

`stt-client` now plays a short chime before muting output and beginning capture.

Purpose:

- Gives nearby people a clear cue that dictation mode is starting.
- Provides immediate feedback that PTT activation succeeded.

Relevant client options/env:

- `--start-chime` / `--no-start-chime` (`STT_START_CHIME`)
- `--chime-backend` (`STT_CHIME_BACKEND`) with `auto/pipewire/paplay/canberra/sounddevice`
- `--chime-file` (`STT_CHIME_FILE`) for file-backed bell playback
- `--chime-sink` (`STT_CHIME_SINK`) for explicit paplay output routing
- `--chime-target` (`STT_CHIME_TARGET`) for explicit PipeWire node routing
- `--chime-role` (`STT_CHIME_ROLE`) for PipeWire stream role control
- `--chime-event-id` (`STT_CHIME_EVENT_ID`)
- `--chime-freq-hz` (`STT_CHIME_FREQ_HZ`)
- `--chime-duration-ms` (`STT_CHIME_DURATION_MS`)
- `--chime-volume` (`STT_CHIME_VOLUME`)
- `--chime-cooldown-ms` (`STT_CHIME_COOLDOWN_MS`)

## Accuracy Tuning Changes (English)

Initial baseline worked but had low accuracy. Current defaults were tuned for English quality:

- `STT_MODEL=large-v3`
- `STT_LANGUAGE=en`
- `STT_BEAM_SIZE=5`
- `STT_BEST_OF=5`
- `STT_VAD_FILTER=1`
- `STT_DEVICE=cuda`
- `STT_COMPUTE_TYPE=float16`

Configured at: `/home/user/.config/stt-daemon.env`

Mirror copy: `/home/user/Documents/stt/config/stt-daemon.env`

## Keybinding State

GNOME custom shortcut configured:

- Name: `STT Push To Talk`
- Command: `/home/user/.local/bin/stt-ptt`
- Binding: `<Primary>grave` (Ctrl+`)

Conflict note:

- No direct GNOME global conflict detected in common keybinding schemas.
- Some applications may use Ctrl+` for app-local actions; GNOME global binding can override those when active.

## Files and Responsibilities

### Runtime source of truth (active)

- `/home/user/.local/lib/stt/stt_daemon.py`
- `/home/user/.local/lib/stt/stt_client.py`
- `/home/user/.local/bin/stt-daemon`
- `/home/user/.local/bin/stt-client`
- `/home/user/.local/bin/stt-ptt`
- `/home/user/.config/stt-daemon.env`
- `/home/user/.config/systemd/user/stt-daemon.service`
- `/home/user/.venvs/faster-whisper/env.sh`

### Development mirror (copied snapshot)

- `/home/user/Documents/stt/lib/*`
- `/home/user/Documents/stt/bin/*`
- `/home/user/Documents/stt/config/*`
- `/home/user/Documents/stt/venv/env.sh`

Important: wrappers inside mirror currently execute scripts in `/home/user/.local/*` and use venv in `/home/user/.venvs/faster-whisper`.

## Operational Commands

### Service lifecycle

```bash
systemctl --user status stt-daemon
systemctl --user restart stt-daemon
journalctl --user -u stt-daemon -f
```

### Basic client use

```bash
stt-client --list-devices
stt-client
stt-client --verbose
```

### PTT behavior tuning

```bash
STT_PTT_DEBOUNCE_MS=900 stt-ptt
STT_TYPE_DELAY_MS=0 stt-ptt
STT_PTT_SEND_ENTER=1 stt-ptt
```

### Mute behavior control

```bash
stt-client --no-mute-output
STT_MUTE_OUTPUT=0 stt-client
```

## Package Dependencies Installed

APT packages added during this work:

- `libportaudio2`
- `pulseaudio-utils` (for `pactl`)
- `xdotool`

Other tools observed already available:

- `ffmpeg`

Additional Python dependency installed:

- `webrtcvad-wheels`

## Known Issues Encountered and Fixes

1. Service restart timeout (daemon hang on shutdown)
   - Symptom: `systemctl --user restart` timed out and force-killed daemon.
   - Cause: calling `server.shutdown()` directly from signal handler thread.
   - Fix: run shutdown via background thread in signal handler.

2. Hallucinated text on silence
   - Symptom: occasional random text from near-silent audio.
   - Fixes:
     - keep daemon-side `vad_filter=1`
     - client now suppresses output when no voice threshold crossing occurred.

3. Audio stayed muted after some runs
   - Symptom: output sinks remained muted.
   - Cause: overlapping client runs racing sink state snapshots/restores.
   - Fix: non-blocking client lock + safer partial-state restore behavior.

4. Repeated delayed outputs after hotkey tests
   - Symptom: multiple transcript bursts after one test period.
   - Cause: key repeat spawning multiple `stt-ptt` invocations.
   - Fix: PTT lock + debounce stamp logic.

5. Unsafe socket path unlink possibility
   - Symptom: daemon startup could unlink arbitrary path if `STT_SOCKET` pointed to a regular file.
   - Fix: explicit stale-socket guard only unlinks existing socket files.

6. Client hang risk if daemon request stalls
   - Symptom: request path could block indefinitely on socket recv/connect.
   - Fix: configurable socket timeout (`STT_SOCKET_TIMEOUT`, `--socket-timeout`).

## Suggested Agent Workflow for Future Changes

1. Edit active files in `/home/user/.local/*` first.
2. Validate syntax after Python edits:

```bash
source /home/user/.venvs/faster-whisper/env.sh
python -m py_compile /home/user/.local/lib/stt/stt_client.py /home/user/.local/lib/stt/stt_daemon.py
```

3. If daemon behavior changed, restart service and verify logs:

```bash
systemctl --user restart stt-daemon
systemctl --user status stt-daemon
journalctl --user -u stt-daemon -n 50 --no-pager
```

4. Smoke-test client and sink restore:

```bash
stt-client --max-seconds 1 --verbose
pactl list short sinks
```

5. Mirror updates to `/home/user/Documents/stt/*` when requested.

## Current Status Snapshot

- STT daemon: running under user systemd.
- Backend: GPU (`cuda`) and English-tuned model settings.
- PTT: active through GNOME global shortcut Ctrl+`.
- Mute-while-listening: implemented and restored safely.
- Concurrency protections: enabled in both client and PTT script.
