# STT Quickstart and Operating Guide

This project provides local, GPU-accelerated speech-to-text for Ubuntu GNOME workflows, with push-to-talk behavior that can type into any focused X11 text field (terminal, browser, editor, chat apps, and more).

For deep implementation history and agent handoff context, read `AGENTS.md`.
For command-only reference, use `CHEATSHEET.md`.

## What You Get

- Persistent `faster-whisper` backend on NVIDIA GPU for low latency after warm-up.
- Microphone capture client with auto-stop on trailing silence.
- Audible start chime before each listen cycle so nearby people can tell dictation is active.
- Optional mute of all audio output during listening to reduce feedback contamination.
- Push-to-talk helper that types transcript text directly into the focused X11 window.
- GNOME global hotkey integration for practical day-to-day use.
- Works beyond GNOME Terminal: browser text areas, web chat inputs, IDE/editor fields, and other focused text boxes.

## Current Runtime State

- Desktop/session: GNOME on X11.
- Backend model defaults: English-focused `large-v3` with stronger decoding search.
- Keybinding: `Ctrl+grave` (`<Primary>grave`) runs `/home/user/.local/bin/stt-ptt`.

## High-Level Architecture

There are 3 layers:

1. `stt-daemon` (always-on backend)
   - Loads Whisper model once and keeps it warm in VRAM.
   - Accepts requests over Unix socket and optional TCP (for Tailnet clients).
   - Returns transcript JSON.

2. `stt-client` (capture/transcribe command)
   - Records mic audio.
   - Plays a short start chime before muting and listening.
   - Applies speech gating to reject environmental noise.
   - Sends local WAV path over Unix socket or WAV bytes to remote TCP server.
   - Prints transcript to stdout.

3. `stt-ptt` (desktop typing integration)
   - Calls `stt-client`.
   - Cleans transcript text.
   - Types text into active window via `xdotool`.
   - Optional Enter key submit.

## System Flow Chart

```mermaid
flowchart LR
    U[User presses PTT\nCtrl+grave] --> P[stt-ptt]
    P --> C1[Play start chime\nlocal audio output]
    C1 --> C2[stt-client capture\nVAD + noise gating]
    C2 --> D{Transport mode}

    D -->|Local default| L1[Unix socket\n~/.cache/stt/faster-whisper.sock]
    L1 --> S[stt-daemon\nGPU faster-whisper model]

    D -->|Remote Tailnet| R1[TCP + auth token\ntcp://100.94.143.124:8765]
    R1 --> S

    S --> T[Transcript JSON response]
    T --> P2[stt-ptt types text\ninto focused X11 field]
    P2 --> A[Terminal / Browser / Editor]
```

## Repository Layout

This repository keeps only project-local paths and templates:

- `lib/` Python implementation (`stt_client.py`, `stt_daemon.py`)
- `bin/` CLI wrappers (`stt-client`, `stt-daemon`, `stt-ptt`)
- `config/` example daemon env configuration
- `venv/` venv activation helper
- `client.env.example` remote client environment template

Runtime install locations (user home paths, service unit paths, and machine-specific env files) are intentionally not listed here.

## Prerequisites and Installed Dependencies

APT packages used:

- `libportaudio2`
- `pulseaudio-utils` (for `pactl`)
- `xdotool`
- `ffmpeg` (already present and useful for tests)

Python packages in STT venv include:

- `faster-whisper`
- `ctranslate2`
- `sounddevice`
- `soundfile`
- `webrtcvad-wheels`
- `nvidia-cublas-cu12`
- `nvidia-cudnn-cu12`

## Service Management

Daemon service is managed by user systemd.

Check status:

```bash
systemctl --user status stt-daemon
```

Restart service:

```bash
systemctl --user restart stt-daemon
```

View logs live:

```bash
journalctl --user -u stt-daemon -f
```

## Centralized GPU over Tailscale

You can run one GPU daemon for your Tailnet and use lightweight clients from other machines.
No separate server app is required; the same `stt-daemon` process can expose both transports.

### Server node (GPU host)

Set in `/home/user/.config/stt-daemon.env`:

```dotenv
STT_SOCKET=~/.cache/stt/faster-whisper.sock
STT_TCP_LISTEN=100.94.143.124
STT_TCP_PORT=8765
STT_SERVER_TOKEN=REPLACE_WITH_LONG_RANDOM_SECRET
STT_MAX_REQUEST_BYTES=10485760
STT_MAX_AUDIO_BYTES=6291456
```

Then restart daemon:

```bash
systemctl --user restart stt-daemon
```

### Client node (any Tailnet machine)

Set client env (shell profile or launcher env):

```bash
export STT_SERVER="tcp://100.94.143.124:8765"
export STT_SERVER_TOKEN="REPLACE_WITH_SAME_SECRET"
```

You can also start from the provided template:

```bash
cp client.env.example .env
```

`stt-client` and `stt-ptt` then keep local capture/chime/typing, while transcription runs on the remote GPU daemon.

## Quickstart (Daily Usage)

1. Ensure daemon is running:

```bash
systemctl --user status stt-daemon
```

2. Optional device check:

```bash
stt-client --list-devices
```

3. Test direct transcription path:

```bash
stt-client --verbose
```

4. Use push-to-talk in any focused text field:

- Focus a text input (terminal, browser, editor, etc.).
- Press `Ctrl+grave` once.
- You should hear a short start chime.
- Speak naturally.
- Pause briefly at sentence end.
- Transcript should be typed into the focused window.

## Where PTT Works

Because `stt-ptt` uses `xdotool type`, it targets the currently focused X11 window, not just GNOME Terminal.

Common targets:

- Terminal prompts (GNOME Terminal, other X11 terminals)
- Browser text fields (search boxes, chat inputs, form text areas)
- Editor and IDE inputs
- Desktop chat apps with standard text input fields

Practical note:

- Click/focus the exact field first, then trigger hotkey.
- If a non-text UI control has focus, typing may be ignored or go to the wrong place.

## Start Chime Behavior

`stt-client` plays an audible chime right before output muting and microphone capture.

Chime backend selection:

- `auto` (default): prefer direct PipeWire playback (`pw-play`), then `paplay`, then synthesized tone, then `canberra`.
- `pipewire`: direct `pw-play` chime path (best fit for modern GNOME).
- `paplay`: plays a bell audio file directly.
- `canberra`: uses desktop event/file playback.
- `sounddevice`: generated high-frequency chirp.

PTT convenience behavior:

- With `pipewire` backend and no `STT_CHIME_TARGET`, `pw-play` uses PipeWire auto-target routing.

Why this exists:

- Signals intent to people nearby so dictation is less likely to be mistaken for normal conversation.
- Gives immediate feedback that the hotkey was accepted.

Default order of operations:

1. Play chime.
2. Optional short cooldown.
3. Mute output sinks (if enabled).
4. Record and transcribe.
5. Restore sink mute state.

## How `stt-client` Detects Speech

The client uses layered gating to avoid triggering on fan noise/hum.

1. WebRTC VAD frame classification
   - Audio is split into small frames (default 20 ms).
   - VAD marks each frame as speech/non-speech.

2. Speech ratio threshold per block
   - For each block, voiced frame ratio must reach `--speech-ratio`.

3. Consecutive speech chunks requirement
   - Requires `--start-speech-chunks` positive blocks before capture starts.

4. Pre-roll buffering
   - Keeps short audio history (`--pre-roll-seconds`) so first words are not cut off.

5. Trailing silence stop
   - Once speech started, stops after `--silence-seconds` of inactivity.

Fallback behavior:

- If WebRTC VAD is unavailable or unsupported at the selected sample rate, it falls back to adaptive RMS thresholding.

## How Output Muting Works

When enabled (default), `stt-client`:

1. Plays the start chime.
2. Enumerates output sinks with `pactl`.
3. Stores each sink's current mute state.
4. Mutes sinks for the capture window.
5. Restores every sink to its original mute state in a `finally` block.

This prevents speaker audio from being interpreted as mic speech and reduces false triggers.

## Concurrency and Repeat Protection

Two lock layers prevent race conditions and repeated delayed output:

- `stt-client` lock (`~/.cache/stt/stt-client.lock`)
  - Prevents overlapping captures from racing sink state.

- `stt-ptt` lock + debounce (`stt-ptt.lock`, `stt-ptt.last`)
  - Prevents key auto-repeat from launching many concurrent jobs.

## Configuration

### Daemon configuration file

Edit `/home/user/.config/stt-daemon.env`.

Current defaults:

```dotenv
STT_MODEL=large-v3
STT_DEVICE=cuda
STT_COMPUTE_TYPE=float16
STT_BEAM_SIZE=5
STT_BEST_OF=5
STT_VAD_FILTER=1
STT_LANGUAGE=en
STT_SOCKET=~/.cache/stt/faster-whisper.sock
STT_TCP_LISTEN=100.94.143.124
STT_TCP_PORT=8765
STT_SERVER_TOKEN=REPLACE_WITH_LONG_RANDOM_SECRET
STT_MAX_REQUEST_BYTES=10485760
STT_MAX_AUDIO_BYTES=6291456
```

After edits, restart daemon:

```bash
systemctl --user restart stt-daemon
```

### Client tuning flags (most important)

- `--webrtcvad` / `--no-webrtcvad`
- `--webrtcvad-mode {0,1,2,3}` (higher = stricter)
- `--speech-ratio` (higher = stricter)
- `--start-speech-chunks` (higher = stricter)
- `--pre-roll-seconds` (larger = safer word onset)
- `--silence-seconds` (larger = waits longer before stopping)
- `--threshold` and `--noise-multiplier` (fallback RMS path)
- `--socket-timeout` (bounds daemon request wait time)
- `--server` (remote endpoint, e.g. `tcp://100.94.143.124:8765`)
- `--server-token` (shared token for remote TCP mode)
- `--server-timeout` (bounds remote connect/read wait time)
- `--mute-start-delay-ms` (delay output muting after capture starts)
- `--start-chime` / `--no-start-chime`
- `--chime-backend {auto,pipewire,paplay,canberra,sounddevice}`
- `--chime-file` (audio file for paplay/canberra backends)
- `--chime-sink` (optional sink name/id for paplay)
- `--chime-target` (optional PipeWire target node serial/name)
- `--chime-role` (PipeWire media role, default `Music`)
- `--chime-event-id` (desktop event id for canberra)
- `--chime-freq-hz`, `--chime-duration-ms`, `--chime-volume`, `--chime-cooldown-ms`

### PTT behavior env vars

- `STT_PTT_DEBOUNCE_MS` (default `0`)
- `STT_TYPE_DELAY_MS` (default `1`)
- `STT_PTT_SEND_ENTER` (`1` to press Enter after typing)
- `STT_CLIENT_BIN` (override client path)
- `STT_SERVER` (remote endpoint, e.g. `tcp://100.94.143.124:8765`)
- `STT_SERVER_TOKEN` (shared token for remote mode)
- `STT_SERVER_TIMEOUT` (remote connect/read timeout)
- `STT_MUTE_START_DELAY_MS` (delay mute start; useful for Bluetooth chime audibility)
- `STT_START_CHIME` (`1`/`0`)
- `STT_CHIME_BACKEND` (`auto`, `pipewire`, `paplay`, `canberra`, `sounddevice`)
- `STT_CHIME_FILE` (default `/usr/share/sounds/freedesktop/stereo/bell.oga`)
- `STT_CHIME_SINK` (optional explicit sink, e.g. `@DEFAULT_SINK@`)
- `STT_CHIME_TARGET` (optional PipeWire target node)
- `STT_CHIME_ROLE` (PipeWire media role, default `Music`)
- `STT_CHIME_EVENT_ID` (default `bell`)
- `STT_CHIME_FREQ_HZ`, `STT_CHIME_DURATION_MS`, `STT_CHIME_VOLUME`, `STT_CHIME_COOLDOWN_MS`

### Wrapper override env vars

Useful if runtime files are relocated:

- `STT_ENV_FILE` (used by `stt-client` and `stt-daemon` wrappers)
- `STT_CLIENT_PY` (override Python client script path)
- `STT_DAEMON_PY` (override Python daemon script path)
- `STT_SOCKET_TIMEOUT` (default client daemon-request timeout)
- `STT_VENV_DIR` (override venv location used by `venv/env.sh`)

## Tuning Recipes

### Balanced default (current)

Good mix of sensitivity and noise rejection:

```bash
stt-client --verbose --webrtcvad-mode 2 --speech-ratio 0.60 --start-speech-chunks 2
```

### Noisy environment (stricter)

```bash
stt-client --verbose --webrtcvad-mode 3 --speech-ratio 0.72 --start-speech-chunks 3
```

### If phrases are being missed (more permissive)

```bash
stt-client --verbose --webrtcvad-mode 1 --speech-ratio 0.50 --start-speech-chunks 1
```

### Longer pause before auto-stop

```bash
stt-client --silence-seconds 1.2
```

### Use a specific microphone

```bash
stt-client --list-devices
stt-client --device 6 --verbose
```

### Disable mute for one run

```bash
stt-client --no-mute-output
```

### Disable chime for one run

```bash
stt-client --no-start-chime
```

### Lower-volume shorter chime

```bash
stt-client --chime-volume 0.10 --chime-duration-ms 80
```

### Force direct PipeWire playback (recommended on modern GNOME)

```bash
stt-client --chime-backend pipewire --chime-file /home/user/.local/share/stt/chime_hi.wav
```

If your notification stream is suppressed, use a role with normal playback priority:

```bash
stt-client --chime-backend pipewire --chime-role Music
```

### Force direct bell-file playback (paplay)

```bash
stt-client --chime-backend paplay --chime-file /usr/share/sounds/freedesktop/stereo/bell.oga
```

### Target a specific output sink for chime

```bash
stt-client --chime-backend paplay --chime-sink @DEFAULT_SINK@
```

### Force GNOME desktop event backend

```bash
stt-client --chime-backend canberra --chime-event-id bell
```

## GNOME Keybinding Notes

Current binding points to:

- `/home/user/.local/bin/stt-ptt`

This PTT path is global for X11 and is not limited to GNOME Terminal; it will type into whichever X11 window currently has keyboard focus.

Check full binding object:

```bash
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/stt-ptt/ command
gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/stt-ptt/ binding
```

Conflict note:

- No known GNOME global conflict was detected for `<Primary>grave` at setup time.
- Some applications may still have app-local meaning for `Ctrl+grave`.

## Troubleshooting

No transcript appears:

- Check daemon: `systemctl --user status stt-daemon`
- Check logs: `journalctl --user -u stt-daemon -n 80 --no-pager`
- Test client directly: `stt-client --verbose`
- Verify socket exists: `/home/user/.cache/stt/faster-whisper.sock`
- Expected secure permissions: socket dir `drwx------`, socket file `srw-------`

Remote mode issues (`STT_SERVER` set):

- Verify TCP listener on server node: `ss -ltn | rg 8765`
- Verify Tailnet reachability: `tailscale ping 100.94.143.124`
- Verify token match on both sides: `STT_SERVER_TOKEN`
- Force explicit call: `STT_SERVER=tcp://100.94.143.124:8765 STT_SERVER_TOKEN=... stt-client --verbose --no-start-chime`

PTT key does nothing:

- Confirm command path in GNOME binding is `/home/user/.local/bin/stt-ptt`.
- Confirm `xdotool` exists: `xdotool -v`.
- Confirm session is X11: `echo "$XDG_SESSION_TYPE"`.

No start chime heard:

- Confirm chime is enabled (`STT_START_CHIME` not set to `0`).
- Confirm system output is not already muted at desktop level.
- Test directly: `stt-client --verbose --max-seconds 0.5`.
- Force PipeWire path: `stt-client --verbose --chime-backend pipewire --max-seconds 0.5`.
- Force bell-file path: `stt-client --verbose --chime-backend paplay --max-seconds 0.5`.
- Force GNOME sound path: `stt-client --verbose --chime-backend canberra --max-seconds 0.5`.
- Check routing: `pactl info | rg "Default Sink"` and set `--chime-sink` / `STT_CHIME_SINK` if needed.
- For PipeWire routing issues: set `STT_CHIME_TARGET` to a concrete node name from `wpctl status`.
- If chime is clipped by immediate mute, set a tiny holdoff: `--chime-cooldown-ms 30` to `60`.

Too many false activations:

- Raise strictness:

```bash
stt-client --webrtcvad-mode 3 --speech-ratio 0.72 --start-speech-chunks 3
```

Speech frequently missed:

- Relax strictness:

```bash
stt-client --webrtcvad-mode 1 --speech-ratio 0.50 --start-speech-chunks 1
```

Audio output remains muted:

- Inspect sink states:

```bash
pactl list short sinks
pactl get-sink-mute <sink-id>
```

- Emergency unmute all sinks:

```bash
for s in $(pactl list short sinks | awk '{print $1}'); do pactl set-sink-mute "$s" 0; done
```

## Development Workflow Notes

When modifying behavior:

1. Edit active runtime files under `/home/user/.local/...`.
2. Validate Python syntax:

```bash
source /home/user/.venvs/faster-whisper/env.sh
python -m py_compile /home/user/.local/lib/stt/stt_client.py /home/user/.local/lib/stt/stt_daemon.py
```

3. Restart and verify daemon:

```bash
systemctl --user restart stt-daemon
systemctl --user status stt-daemon
```

4. Run smoke test:

```bash
stt-client --verbose --max-seconds 1.5
```

5. Sync mirror files into `/home/user/Documents/stt/*` when needed.
