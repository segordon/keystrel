# STT Cheat Sheet

Fast command reference for the local GPU STT stack.

## Daily Use

Check daemon:

```bash
systemctl --user status stt-daemon
```

Restart daemon:

```bash
systemctl --user restart stt-daemon
```

Tail daemon logs:

```bash
journalctl --user -u stt-daemon -f
```

Basic STT test:

```bash
stt-client --verbose
```

List mic devices:

```bash
stt-client --list-devices
```

Use specific mic:

```bash
stt-client --device 6 --verbose
```

## Tailscale Remote Mode

Server node (`/home/user/.config/stt-daemon.env`):

```dotenv
STT_TCP_LISTEN=100.94.143.124
STT_TCP_PORT=8765
STT_SERVER_TOKEN=REPLACE_WITH_LONG_RANDOM_SECRET
```

Restart server daemon:

```bash
systemctl --user restart stt-daemon
```

Client node env:

```bash
export STT_SERVER="tcp://100.94.143.124:8765"
export STT_SERVER_TOKEN="REPLACE_WITH_SAME_SECRET"
```

Or copy template:

```bash
cp client.env.example .env
```

Remote client test:

```bash
stt-client --verbose --no-start-chime
```

## Push To Talk

Current GNOME hotkey:

- `Ctrl+grave` (`<Primary>grave`)

Works in any focused X11 text field, including browser inputs (not just GNOME Terminal).
PTT now plays a short start chime before muting/listening.
Default `auto` backend prefers direct PipeWire playback (`pw-play`).

Binding points to:

- `/home/user/.local/bin/stt-ptt`

Check keybinding:

```bash
gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/stt-ptt/ binding
gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/stt-ptt/ command
```

Run PTT script manually:

```bash
stt-ptt
```

Auto-press Enter after typing:

```bash
STT_PTT_SEND_ENTER=1 stt-ptt
```

Disable chime for one run:

```bash
stt-client --no-start-chime
```

Force direct PipeWire playback (recommended on modern GNOME):

```bash
stt-client --chime-backend pipewire --chime-file /home/user/.local/share/stt/chime_hi.wav
```

Use a non-notification role for audibility:

```bash
stt-client --chime-backend pipewire --chime-role Music
```

Pin PipeWire chime to a specific node target:

```bash
stt-client --chime-backend pipewire --chime-target bluez_output.FC_58_FA_62_40_04.1
```

Force direct bell-file playback (paplay):

```bash
stt-client --chime-backend paplay --chime-file /usr/share/sounds/freedesktop/stereo/bell.oga
```

Target a specific sink for chime playback:

```bash
stt-client --chime-backend paplay --chime-sink @DEFAULT_SINK@
```

Force GNOME desktop chime backend:

```bash
stt-client --chime-backend canberra --chime-event-id bell
```

## Noise and Sensitivity Tuning

Current balanced profile:

```bash
stt-client --verbose --webrtcvad-mode 2 --speech-ratio 0.60 --start-speech-chunks 2
```

Stricter (more noise rejection):

```bash
stt-client --verbose --webrtcvad-mode 3 --speech-ratio 0.72 --start-speech-chunks 3
```

More permissive (catches quieter speech):

```bash
stt-client --verbose --webrtcvad-mode 1 --speech-ratio 0.50 --start-speech-chunks 1
```

Bound daemon request wait time:

```bash
stt-client --socket-timeout 15
```

Wait longer before stopping:

```bash
stt-client --silence-seconds 1.2
```

Disable output muting for one run:

```bash
stt-client --no-mute-output
```

Adjust chime level/duration:

```bash
stt-client --chime-volume 0.10 --chime-duration-ms 80
```

## Common Fixes

Check session type (PTT typing currently expects X11):

```bash
echo "$XDG_SESSION_TYPE"
```

Verify socket exists:

```bash
ls -l /home/user/.cache/stt/faster-whisper.sock
```

Verify Tailnet TCP listener on server:

```bash
ss -ltn | rg 8765
```

Remote mode quick check from client node:

```bash
STT_SERVER=tcp://100.94.143.124:8765 STT_SERVER_TOKEN=... stt-client --verbose --no-start-chime
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

- `/home/user/.config/stt-daemon.env`

Service unit:

- `/home/user/.config/systemd/user/stt-daemon.service`

Runtime scripts:

- `/home/user/.local/bin/stt-daemon`
- `/home/user/.local/bin/stt-client`
- `/home/user/.local/bin/stt-ptt`
- `/home/user/.local/lib/stt/stt_daemon.py`
- `/home/user/.local/lib/stt/stt_client.py`

Wrapper override env vars:

- `STT_ENV_FILE`
- `STT_CLIENT_PY`
- `STT_DAEMON_PY`
- `STT_SOCKET_TIMEOUT`
- `STT_SERVER`
- `STT_SERVER_TOKEN`
- `STT_SERVER_TIMEOUT`
- `STT_VENV_DIR`
- `STT_TCP_LISTEN`
- `STT_TCP_PORT`
- `STT_MAX_REQUEST_BYTES`
- `STT_MAX_AUDIO_BYTES`
- `STT_START_CHIME`
- `STT_CHIME_BACKEND`
- `STT_CHIME_FILE`
- `STT_CHIME_SINK`
- `STT_CHIME_TARGET`
- `STT_CHIME_ROLE`
- `STT_CHIME_EVENT_ID`
- `STT_CHIME_FREQ_HZ`
- `STT_CHIME_DURATION_MS`
- `STT_CHIME_VOLUME`
- `STT_CHIME_COOLDOWN_MS`

After changing daemon config:

```bash
systemctl --user restart stt-daemon
```
