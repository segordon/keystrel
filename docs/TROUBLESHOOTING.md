# Keystrel Troubleshooting

## No Transcript Appears

- Check daemon state:

```bash
systemctl --user status keystrel-daemon
```

- Check daemon logs:

```bash
journalctl --user -u keystrel-daemon -n 80 --no-pager
```

- Test client directly:

```bash
keystrel-client --verbose
```

- Verify socket exists:

```bash
ls -l "$HOME/.cache/keystrel/faster-whisper.sock"
```

Expected secure permissions:

- socket directory: `drwx------`
- socket file: `srw-------`

## Remote Mode Issues (`KEYSTREL_SERVER` Set)

- Verify TCP listener on server:

```bash
ss -ltn | rg 8765
```

- Verify Tailnet reachability:

```bash
tailscale ping <tailscale-ip>
```

- Verify token matches on both nodes (`KEYSTREL_SERVER_TOKEN`).

- Run explicit client test:

```bash
KEYSTREL_SERVER=tcp://<tailscale-ip>:8765 KEYSTREL_SERVER_TOKEN=... keystrel-client --verbose --no-start-chime
```

## PTT Key Does Nothing

- Confirm hotkey command points to `$HOME/.local/bin/keystrel-ptt`.
- Confirm `xdotool` exists:

```bash
xdotool -v
```

- Confirm X11 session:

```bash
echo "$XDG_SESSION_TYPE"
```

## No Start Chime Heard

- Confirm chime is enabled (`KEYSTREL_START_CHIME` not `0`).
- Confirm desktop output is not already muted.

- Test with verbose client run:

```bash
keystrel-client --verbose --max-seconds 0.5
```

- Force each backend to isolate routing problems:

```bash
keystrel-client --verbose --chime-backend pipewire --max-seconds 0.5
keystrel-client --verbose --chime-backend paplay --max-seconds 0.5
keystrel-client --verbose --chime-backend canberra --max-seconds 0.5
```

- Inspect sink routing:

```bash
pactl info | rg "Default Sink"
```

- For PipeWire routing issues, set `KEYSTREL_CHIME_TARGET` to a concrete node from `wpctl status`.
- If chime is clipped by immediate mute, try `--chime-cooldown-ms 30` to `60`.

## Too Many False Activations

Increase strictness:

```bash
keystrel-client --webrtcvad-mode 3 --speech-ratio 0.72 --start-speech-chunks 3
```

## Speech Frequently Missed

Relax strictness:

```bash
keystrel-client --webrtcvad-mode 1 --speech-ratio 0.50 --start-speech-chunks 1
```

## Audio Output Remains Muted

- Inspect sink states:

```bash
pactl list short sinks
pactl get-sink-mute <sink-id>
```

- Emergency unmute all sinks:

```bash
for s in $(pactl list short sinks | awk '{print $1}'); do pactl set-sink-mute "$s" 0; done
```
