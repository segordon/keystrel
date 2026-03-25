# Keystrel Cheat Sheet

Fast command reference for the local Keystrel stack.

Need fuller context? Start at `docs/README.md`.

## Daily Use

Check daemon:

```bash
systemctl --user status keystrel-daemon
```

Restart daemon:

```bash
systemctl --user restart keystrel-daemon
```

Tail daemon logs:

```bash
journalctl --user -u keystrel-daemon -f
```

Basic Keystrel test:

```bash
keystrel-client --verbose
```

List mic devices:

```bash
keystrel-client --list-devices
```

Use specific mic:

```bash
keystrel-client --device <device-id-or-name> --sample-rate 48000 --verbose
```

Run unit tests:

```bash
python -m unittest discover -s tests -v
```

Run Python syntax checks:

```bash
python -m py_compile lib/keystrel_client.py lib/keystrel_daemon.py
```

## Tailscale Remote Mode

Server node (`$HOME/.config/keystrel-daemon.env`):

```dotenv
KEYSTREL_TCP_LISTEN=<tailscale-ip>
KEYSTREL_TCP_PORT=8765
KEYSTREL_SERVER_TOKEN=REPLACE_WITH_LONG_RANDOM_SECRET
```

Find `<tailscale-ip>`:

```bash
tailscale ip -4
```

Restart server daemon:

```bash
systemctl --user restart keystrel-daemon
```

Client node env:

```bash
export KEYSTREL_SERVER="tcp://<tailscale-ip>:8765"
export KEYSTREL_SERVER_TOKEN="REPLACE_WITH_SAME_SECRET"
```

Or copy template:

```bash
cp keystrel-client.env.example .env
```

Remote client test:

```bash
keystrel-client --verbose --no-start-chime
```

## Push To Talk

Current hotkey example:

- `Ctrl+grave` (`<Primary>grave`)

Works in any focused X11 text field, including browser inputs.
PTT now plays a short start chime before muting/listening.
Default `auto` backend prefers direct PipeWire playback (`pw-play`).

Binding points to:

- `$HOME/.local/bin/keystrel-ptt`

Check keybinding:

```bash
# command: $HOME/.local/bin/keystrel-ptt
# binding: Ctrl+grave (or any preferred global hotkey)
```

Run PTT script manually:

```bash
keystrel-ptt
```

Cancel active capture early:

- Press the same PTT hotkey again while capture is active.

Auto-press Enter after typing:

```bash
KEYSTREL_PTT_SEND_ENTER=1 keystrel-ptt
```

Tune repeat/cancel debounce behavior:

```bash
KEYSTREL_PTT_DEBOUNCE_MS=180 KEYSTREL_PTT_CANCEL_DEBOUNCE_MS=350 keystrel-ptt
```

Disable chime for one run:

```bash
keystrel-client --no-start-chime
```

Force direct PipeWire playback (recommended on modern Linux desktops):

```bash
keystrel-client --chime-backend pipewire --chime-file "$HOME/.local/share/keystrel/chime_hi.wav"
```

Use a non-notification role for audibility:

```bash
keystrel-client --chime-backend pipewire --chime-role Music
```

Pin PipeWire chime to a specific node target:

```bash
keystrel-client --chime-backend pipewire --chime-target bluez_output.FC_58_FA_62_40_04.1
```

Force direct bell-file playback (paplay):

```bash
keystrel-client --chime-backend paplay --chime-file /usr/share/sounds/freedesktop/stereo/bell.oga
```

Target a specific sink for chime playback:

```bash
keystrel-client --chime-backend paplay --chime-sink @DEFAULT_SINK@
```

Force desktop event chime backend:

```bash
keystrel-client --chime-backend canberra --chime-event-id bell
```

## Noise and Sensitivity Tuning

Current balanced profile:

```bash
keystrel-client --verbose --webrtcvad-mode 2 --speech-ratio 0.60 --start-speech-chunks 2
```

Stricter (more noise rejection):

```bash
keystrel-client --verbose --webrtcvad-mode 3 --speech-ratio 0.72 --start-speech-chunks 3
```

More permissive (catches quieter speech):

```bash
keystrel-client --verbose --webrtcvad-mode 1 --speech-ratio 0.50 --start-speech-chunks 1
```

Bound daemon request wait time:

```bash
keystrel-client --socket-timeout 15
```

Wait longer before stopping:

```bash
keystrel-client --silence-seconds 1.2
```

Disable output muting for one run:

```bash
keystrel-client --no-mute-output
```

Adjust chime level/duration:

```bash
keystrel-client --chime-volume 0.10 --chime-duration-ms 80
```

## Common Fixes

Check session type (PTT typing currently expects X11):

```bash
echo "$XDG_SESSION_TYPE"
```

Verify socket exists:

```bash
ls -l "$HOME/.cache/keystrel/faster-whisper.sock"
```

Verify Tailnet TCP listener on server:

```bash
ss -ltn | rg 8765
```

Remote mode quick check from client node:

```bash
KEYSTREL_SERVER=tcp://<tailscale-ip>:8765 KEYSTREL_SERVER_TOKEN=... keystrel-client --verbose --no-start-chime
```

Expected secure permissions:

- socket dir: `drwx------`
- socket file: `srw-------`

Emergency unmute all sinks:

```bash
for s in $(pactl list short sinks | awk '{print $1}'); do pactl set-sink-mute "$s" 0; done
```

Inspect sink mute states:

```bash
pactl list short sinks
pactl get-sink-mute <sink-id>
```

## Config Files

Daemon config:

- `$HOME/.config/keystrel-daemon.env`

Service unit:

- `$HOME/.config/systemd/user/keystrel-daemon.service`

Runtime scripts:

- `$HOME/.local/bin/keystrel-daemon`
- `$HOME/.local/bin/keystrel-client`
- `$HOME/.local/bin/keystrel-ptt`
- `$HOME/.local/lib/keystrel/keystrel_daemon.py`
- `$HOME/.local/lib/keystrel/keystrel_client.py`

Wrapper override env vars:

- `KEYSTREL_ENV_FILE`
- `KEYSTREL_CLIENT_PY`
- `KEYSTREL_DAEMON_PY`
- `KEYSTREL_SOCKET_TIMEOUT`
- `KEYSTREL_SERVER`
- `KEYSTREL_SERVER_TOKEN`
- `KEYSTREL_SERVER_TIMEOUT`
- `KEYSTREL_VENV_DIR`
- `KEYSTREL_TCP_LISTEN`
- `KEYSTREL_TCP_PORT`
- `KEYSTREL_MAX_REQUEST_BYTES`
- `KEYSTREL_MAX_AUDIO_BYTES`
- `KEYSTREL_START_CHIME`
- `KEYSTREL_CHIME_BACKEND`
- `KEYSTREL_CHIME_FILE`
- `KEYSTREL_CHIME_SINK`
- `KEYSTREL_CHIME_TARGET`
- `KEYSTREL_CHIME_ROLE`
- `KEYSTREL_CHIME_EVENT_ID`
- `KEYSTREL_CHIME_FREQ_HZ`
- `KEYSTREL_CHIME_DURATION_MS`
- `KEYSTREL_CHIME_VOLUME`
- `KEYSTREL_CHIME_COOLDOWN_MS`

After changing daemon config:

```bash
systemctl --user restart keystrel-daemon
```
