# Keystrel Configuration and Tuning

This guide collects daemon/client/PTT settings and practical tuning recipes.

## Daemon Configuration File

Edit:

- `$HOME/.config/keystrel-daemon.env`

Example baseline:

```dotenv
KEYSTREL_MODEL=large-v3
KEYSTREL_DEVICE=cuda
KEYSTREL_COMPUTE_TYPE=float16
KEYSTREL_BEAM_SIZE=5
KEYSTREL_BEST_OF=5
KEYSTREL_VAD_FILTER=1
KEYSTREL_LANGUAGE=en
KEYSTREL_SOCKET=~/.cache/keystrel/faster-whisper.sock
KEYSTREL_TCP_LISTEN=<tailscale-ip>
KEYSTREL_TCP_PORT=8765
KEYSTREL_SERVER_TOKEN=REPLACE_WITH_LONG_RANDOM_SECRET
KEYSTREL_MAX_REQUEST_BYTES=10485760
KEYSTREL_MAX_AUDIO_BYTES=6291456
```

Apply changes:

```bash
systemctl --user restart keystrel-daemon
```

## Client Tuning Flags (Most Relevant)

- `--webrtcvad` / `--no-webrtcvad`
- `--webrtcvad-mode {0,1,2,3}`
- `--speech-ratio`
- `--start-speech-chunks`
- `--pre-roll-seconds`
- `--silence-seconds`
- `--threshold`
- `--noise-multiplier`
- `--socket-timeout`
- `--server`
- `--server-token`
- `--server-timeout`
- `--mute-start-delay-ms`
- `--mute-settle-ms`
- `--start-chime` / `--no-start-chime`
- `--chime-backend {auto,pipewire,paplay,canberra,sounddevice}`
- `--chime-file`
- `--chime-sink`
- `--chime-target`
- `--chime-role`
- `--chime-event-id`
- `--chime-freq-hz`
- `--chime-duration-ms`
- `--chime-volume`
- `--chime-cooldown-ms`

## PTT and Client Environment Variables

PTT behavior:

- `KEYSTREL_PTT_DEBOUNCE_MS`
- `KEYSTREL_PTT_DOUBLE_PRESS_CANCEL`
- `KEYSTREL_PTT_CANCEL_DEBOUNCE_MS`
- `KEYSTREL_TYPE_DELAY_MS`
- `KEYSTREL_PTT_SEND_ENTER`
- `KEYSTREL_CLIENT_BIN`
- `KEYSTREL_PTT_MUTE_START_DELAY_MS`
- `KEYSTREL_PTT_CHIME_ENABLED`
- `KEYSTREL_PTT_CHIME_BACKEND`
- `KEYSTREL_PTT_CHIME_FILE`
- `KEYSTREL_PTT_CHIME_TARGET`
- `KEYSTREL_PTT_CHIME_ROLE`
- `KEYSTREL_PTT_CHIME_VOLUME`
- `KEYSTREL_PTT_CHIME_COOLDOWN_MS`

Notes:

- `KEYSTREL_PTT_DOUBLE_PRESS_CANCEL` defaults to `1`.
- `KEYSTREL_PTT_DEBOUNCE_MS` defaults to `180`.
- `KEYSTREL_PTT_CANCEL_DEBOUNCE_MS` defaults to `350`.

Client/server transport and capture:

- `KEYSTREL_SERVER`
- `KEYSTREL_SERVER_TOKEN`
- `KEYSTREL_SERVER_TIMEOUT`
- `KEYSTREL_SOCKET_TIMEOUT`
- `KEYSTREL_INPUT_DEVICE`
- `KEYSTREL_SAMPLE_RATE`
- `KEYSTREL_MUTE_START_DELAY_MS`
- `KEYSTREL_MUTE_SETTLE_MS`
- `KEYSTREL_MUTE_OUTPUT`
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

Wrapper overrides:

- `KEYSTREL_ENV_FILE`
- `KEYSTREL_CLIENT_PY`
- `KEYSTREL_DAEMON_PY`
- `KEYSTREL_VENV_DIR`

## Tuning Recipes

Balanced baseline:

```bash
keystrel-client --verbose --webrtcvad-mode 2 --speech-ratio 0.60 --start-speech-chunks 2
```

Noisy environment (stricter):

```bash
keystrel-client --verbose --webrtcvad-mode 3 --speech-ratio 0.72 --start-speech-chunks 3
```

Permissive profile (catch quiet speech):

```bash
keystrel-client --verbose --webrtcvad-mode 1 --speech-ratio 0.50 --start-speech-chunks 1
```

Longer trailing pause before stop:

```bash
keystrel-client --silence-seconds 1.2
```

Use a dedicated mic:

```bash
keystrel-client --list-devices
keystrel-client --device <device-id-or-name> --sample-rate 48000 --verbose
```

Prefer input-only devices when possible (for example USB mic with `1 in, 0 out`).

Disable output muting for one run:

```bash
keystrel-client --no-mute-output
```

Disable start chime for one run:

```bash
keystrel-client --no-start-chime
```

Lower-volume shorter chime:

```bash
keystrel-client --chime-volume 0.10 --chime-duration-ms 80
```

Force PipeWire chime backend:

```bash
keystrel-client --chime-backend pipewire --chime-file "$HOME/.local/share/keystrel/chime_hi.wav"
```

Use a non-notification role for audibility:

```bash
keystrel-client --chime-backend pipewire --chime-role Music
```

Force paplay backend:

```bash
keystrel-client --chime-backend paplay --chime-file /usr/share/sounds/freedesktop/stereo/bell.oga
```

Target specific sink for chime:

```bash
keystrel-client --chime-backend paplay --chime-sink @DEFAULT_SINK@
```

Force canberra backend:

```bash
keystrel-client --chime-backend canberra --chime-event-id bell
```
