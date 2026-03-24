# Keystrel Project Agent Guide

This file documents what has been built so far for the Ubuntu speech-to-text workflow and how future agents should work on it safely.

## Project Goal

Provide fast, local speech-to-text (STT) for terminal workflows (including interactive prompt tools), with practical push-to-talk behavior and no dependency on piping stdin into the app.

## Naming Status

- Project name: `Keystrel`
- Primary commands: `keystrel-daemon`, `keystrel-client`, `keystrel-ptt`
- Preferred env var prefix: `KEYSTREL_`
- Legacy `STT_` env vars remain supported for migration safety (with one-time deprecation warnings)

## Cost Policy

- Do not use GitHub Actions for this repository.
- Do not use or enable any GitHub feature that may incur cost.
- Do not add CI workflows under `.github/workflows/`.
- Keep testing and automation local (CLI commands documented in repo docs).

## Current Environment

- OS/session: Ubuntu derivative, X11 session.
- Runtime target: any host where `faster-whisper` runs.
- Python: 3.12.
- Transcription backend: `faster-whisper` via `ctranslate2`.

## What Was Implemented

### 1) faster-whisper backend install and validation

- Created venv at `$HOME/.venvs/faster-whisper`.
- Installed core Python packages:
  - `faster-whisper`
  - `sounddevice`
  - `soundfile`
  - host-specific accelerator/runtime dependencies as needed
- Added env helper: `$HOME/.venvs/faster-whisper/env.sh`
  - exports `VIRTUAL_ENV`
  - prepends `PATH`
  - supports runtime library path overrides when needed
- Verified:
  - model load/transcription path succeeds

### 2) Warm daemon architecture

- Added daemon script: `$HOME/.local/lib/keystrel/keystrel_daemon.py`
- Daemon runs a persistent Whisper model and serves requests over Unix socket and optional TCP.
- Socket default: `$HOME/.cache/keystrel/faster-whisper.sock`
- TCP defaults (when enabled): `<tailscale-ip>:8765`
- Protocol: single-line JSON request/response over AF_UNIX or TCP stream.

Unix request (minimum):

```json
{"audio_path":"/tmp/file.wav"}
```

TCP request (minimum):

```json
{"audio_b64":"<base64 wav bytes>","auth_token":"<shared-token>"}
```

Optional request fields supported:

- `language`
- `task`
- `vad_filter`
- `beam_size`
- `best_of`

TCP safeguards:

- requires `auth_token` that matches `KEYSTREL_SERVER_TOKEN`
- rejects `audio_path` in TCP mode
- enforces request/audio size limits (`KEYSTREL_MAX_REQUEST_BYTES`, `KEYSTREL_MAX_AUDIO_BYTES`)

Response contains:

- `ok`
- `text`
- `language`
- `language_probability`
- `elapsed_s`

### 3) Client capture tool

- Added client script: `$HOME/.local/lib/keystrel/keystrel_client.py`
- Captures mic audio with `sounddevice`, VAD-like stop logic based on RMS threshold + trailing silence.
- Writes temp WAV, sends to local daemon (Unix) or remote daemon (TCP), prints transcript to stdout.

Key behavior:

- Returns empty output if voice was never detected (`started_voice` guard).
- Supports overrides for language/vad/beam/best_of.
- Supports device listing via `--list-devices`.
- Supports remote transport with `KEYSTREL_SERVER=tcp://<host>:<port>` and `KEYSTREL_SERVER_TOKEN`.
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
- Default mute behavior currently enabled via env default logic (`KEYSTREL_MUTE_OUTPUT` falls back to `1`).

### 5) Concurrency/race protections

Two separate protections were added to fix real issues seen in testing.

1. Client lock (`keystrel_client.py`)
   - Non-blocking file lock at `~/.cache/keystrel/keystrel-client.lock`.
   - If a capture is already active, new run exits quietly.
   - Prevents overlapping captures and sink-state races.

2. PTT lock + debounce (`keystrel-ptt`)
   - Lock file: `${XDG_RUNTIME_DIR:-$HOME/.cache/keystrel}/keystrel-ptt.lock`
   - Debounce stamp: `${XDG_RUNTIME_DIR:-$HOME/.cache/keystrel}/keystrel-ptt.last`
   - Default debounce: `0ms` via `KEYSTREL_PTT_DEBOUNCE_MS` (lock handles overlap protection)
   - Prevents key-repeat spawning multiple jobs and delayed/batched output typing.

### 6) PTT integration for X11 text fields

- Installed `xdotool` and created launcher: `$HOME/.local/bin/keystrel-ptt`
- Flow:
  1. run `keystrel-client`
  2. sanitize transcript (CR/LF)
  3. type into focused window with `xdotool type`
  4. optional Enter key if `KEYSTREL_PTT_SEND_ENTER=1`

PTT script safety checks:

- requires X11 session
- requires executable `keystrel-client`
- requires `xdotool`
- requires `flock`

### 7) Systemd user service

- Service file: `$HOME/.config/systemd/user/keystrel-daemon.service`
- Env file: `$HOME/.config/keystrel-daemon.env`
- Wrapper: `$HOME/.local/bin/keystrel-daemon`
- Service is enabled and started (`systemctl --user`).

### 8) Additional noise suppression tuning

Client defaults were hardened against steady ambient noise (fan/hum):

- `KEYSTREL_WEBRTCVAD=1`
- `KEYSTREL_WEBRTCVAD_MODE=2`
- `KEYSTREL_WEBRTCVAD_FRAME_MS=20`
- `KEYSTREL_SPEECH_RATIO=0.60`
- `KEYSTREL_START_SPEECH_CHUNKS=2`
- `KEYSTREL_PRE_ROLL_SECONDS=0.35`

These settings reduce false speech starts while still keeping spoken onset via pre-roll.

### 9) Runtime hardening pass

Additional robustness/security improvements were applied:

- Daemon now refuses to unlink non-socket paths when preparing the socket path.
- Daemon socket directory/file permissions are tightened (`0700` dir under home, `0600` socket file).
- Client now supports daemon socket timeout (`--socket-timeout`, env `KEYSTREL_SOCKET_TIMEOUT`).
- Client/daemon env parsing now tolerates malformed env values and falls back to defaults (with warnings) instead of traceback crashes.
- Wrapper scripts were de-hardcoded to use `$HOME` and overridable env paths.
- PTT debounce/type delay env values are validated before use.
- Remote TCP requests now require shared-token auth and bounded payload size.

### 10) Audible start chime before listen

`keystrel-client` now plays a short chime before muting output and beginning capture.

Purpose:

- Gives nearby people a clear cue that dictation mode is starting.
- Provides immediate feedback that PTT activation succeeded.

Relevant client options/env:

- `--start-chime` / `--no-start-chime` (`KEYSTREL_START_CHIME`)
- `--chime-backend` (`KEYSTREL_CHIME_BACKEND`) with `auto/pipewire/paplay/canberra/sounddevice`
- `--chime-file` (`KEYSTREL_CHIME_FILE`) for file-backed bell playback
- `--chime-sink` (`KEYSTREL_CHIME_SINK`) for explicit paplay output routing
- `--chime-target` (`KEYSTREL_CHIME_TARGET`) for explicit PipeWire node routing
- `--chime-role` (`KEYSTREL_CHIME_ROLE`) for PipeWire stream role control
- `--chime-event-id` (`KEYSTREL_CHIME_EVENT_ID`)
- `--chime-freq-hz` (`KEYSTREL_CHIME_FREQ_HZ`)
- `--chime-duration-ms` (`KEYSTREL_CHIME_DURATION_MS`)
- `--chime-volume` (`KEYSTREL_CHIME_VOLUME`)
- `--chime-cooldown-ms` (`KEYSTREL_CHIME_COOLDOWN_MS`)

### 11) Centralized inference over Tailscale

The same daemon process now supports optional TCP transport for Tailnet clients.

Server-side env (daemon host):

- `KEYSTREL_TCP_LISTEN=<tailscale-ip>`
- `KEYSTREL_TCP_PORT=8765`
- `KEYSTREL_SERVER_TOKEN=<long-random-secret>`
- `KEYSTREL_MAX_REQUEST_BYTES=10485760`
- `KEYSTREL_MAX_AUDIO_BYTES=6291456`

Client-side env (any node):

- `KEYSTREL_SERVER=tcp://<tailscale-ip>:8765`
- `KEYSTREL_SERVER_TOKEN=<same-secret>`
- `KEYSTREL_SERVER_TIMEOUT=<seconds>`

Important security note:

- Never commit real `KEYSTREL_SERVER_TOKEN` values to git.

## Accuracy Tuning Changes (English)

Initial baseline worked but had low accuracy. Current defaults were tuned for English quality:

- `KEYSTREL_MODEL=large-v3`
- `KEYSTREL_LANGUAGE=en`
- `KEYSTREL_BEAM_SIZE=5`
- `KEYSTREL_BEST_OF=5`
- `KEYSTREL_VAD_FILTER=1`
- `KEYSTREL_DEVICE=cuda`
- `KEYSTREL_COMPUTE_TYPE=float16`

Configured at: `$HOME/.config/keystrel-daemon.env`

Mirror copy: `<repo-root>/config/keystrel-daemon.env`

## Keybinding State

Global shortcut configured:

- Name: `Keystrel Push To Talk`
- Command: `$HOME/.local/bin/keystrel-ptt`
- Binding: `<Primary>grave` (Ctrl+`)

Conflict note:

- No direct desktop-global conflict detected in common keybinding schemas.
- Some applications may use Ctrl+` for app-local actions; the global binding can override those when active.

## Files and Responsibilities

### Runtime source of truth (active)

- `$HOME/.local/lib/keystrel/keystrel_daemon.py`
- `$HOME/.local/lib/keystrel/keystrel_client.py`
- `$HOME/.local/bin/keystrel-daemon`
- `$HOME/.local/bin/keystrel-client`
- `$HOME/.local/bin/keystrel-ptt`
- `$HOME/.config/keystrel-daemon.env`
- `$HOME/.config/systemd/user/keystrel-daemon.service`
- `$HOME/.venvs/faster-whisper/env.sh`

### Development mirror (copied snapshot)

- `<repo-root>/lib/*`
- `<repo-root>/bin/*`
- `<repo-root>/config/*`
- `<repo-root>/venv/env.sh`

Important: wrappers inside mirror currently execute scripts in `$HOME/.local/*` and use venv in `$HOME/.venvs/faster-whisper`.

## Operational Commands

### Service lifecycle

```bash
systemctl --user status keystrel-daemon
systemctl --user restart keystrel-daemon
journalctl --user -u keystrel-daemon -f
```

### Basic client use

```bash
keystrel-client --list-devices
keystrel-client
keystrel-client --verbose
```

### PTT behavior tuning

```bash
KEYSTREL_PTT_DEBOUNCE_MS=0 keystrel-ptt
KEYSTREL_TYPE_DELAY_MS=0 keystrel-ptt
KEYSTREL_PTT_SEND_ENTER=1 keystrel-ptt
```

### Mute behavior control

```bash
keystrel-client --no-mute-output
KEYSTREL_MUTE_OUTPUT=0 keystrel-client
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
   - Cause: key repeat spawning multiple `keystrel-ptt` invocations.
   - Fix: PTT lock + debounce stamp logic.

5. Unsafe socket path unlink possibility
   - Symptom: daemon startup could unlink arbitrary path if `KEYSTREL_SOCKET` pointed to a regular file.
   - Fix: explicit stale-socket guard only unlinks existing socket files.

6. Client hang risk if daemon request stalls
   - Symptom: request path could block indefinitely on socket recv/connect.
   - Fix: configurable socket timeout (`KEYSTREL_SOCKET_TIMEOUT`, `--socket-timeout`).

## Suggested Agent Workflow for Future Changes

1. Edit active files in `$HOME/.local/*` first.
2. Validate syntax after Python edits:

```bash
source "$HOME/.venvs/faster-whisper/env.sh"
python -m py_compile "$HOME/.local/lib/keystrel/keystrel_client.py" "$HOME/.local/lib/keystrel/keystrel_daemon.py"
```

3. If daemon behavior changed, restart service and verify logs:

```bash
systemctl --user restart keystrel-daemon
systemctl --user status keystrel-daemon
journalctl --user -u keystrel-daemon -n 50 --no-pager
```

4. Smoke-test client and sink restore:

```bash
keystrel-client --max-seconds 1 --verbose
pactl list short sinks
```

5. Mirror updates to `<repo-root>/*` when requested.

## Current Status Snapshot

- Keystrel daemon: running under user systemd.
- Backend: `faster-whisper` runtime configured for host-supported device settings.
- PTT: active through a global shortcut (example: Ctrl+`).
- Mute-while-listening: implemented and restored safely.
- Concurrency protections: enabled in both client and PTT script.
