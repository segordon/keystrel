# Keystrel Testing Guide

This document defines the repeatable test flow now that feature work is paused.

## 1) Fast Regression Checks

Run these first after any code or config change:

```bash
python -m unittest discover -s tests -v
python -m py_compile lib/keystrel_client.py lib/keystrel_daemon.py
```

What unit tests currently cover:

- `KEYSTREL_SERVER` endpoint parsing and validation rules.
- Client request option payload construction.
- Client CLI main-path behavior and exit codes for common failure modes.
- Client Unix/TCP request error handling (timeouts, empty/invalid/oversize responses).
- Client output mute/restore logic and chime backend fallback order.
- Client non-blocking lock behavior.
- Unix-socket request validation and local `audio_path` handling.
- TCP transport auth checks (missing token, bad token).
- TCP request/audio payload size limits.
- TCP behavior for disallowed `audio_path` and valid `audio_b64` flow.
- TCP repeated-request soak behavior and temp-file cleanup.
- Daemon socket path safety behavior for stale/missing/non-socket paths.
- Daemon startup guards (`no transport`, `invalid port`, `missing token`) and dual-transport shutdown path.
- `keystrel-ptt` script debounce and overlap lock behavior.

## 2) Local Runtime Smoke Test (Unix Socket)

Server node:

```bash
systemctl --user restart keystrel-daemon
systemctl --user is-active keystrel-daemon
```

Client smoke test:

```bash
keystrel-client --verbose --max-seconds 1.5
```

Expected:

- service is `active`
- client returns without crash/hang
- transcript output is printed (or empty on silence)

## 3) Remote Runtime Smoke Test (Tailscale TCP)

On server node:

1. Find Tailnet IP:

```bash
tailscale ip -4
```

2. Set daemon env (`$HOME/.config/keystrel-daemon.env`):

```dotenv
KEYSTREL_TCP_LISTEN=<tailscale-ip>
KEYSTREL_TCP_PORT=8765
KEYSTREL_SERVER_TOKEN=<shared-secret>
```

3. Restart and verify listener:

```bash
systemctl --user restart keystrel-daemon
ss -ltn | rg 8765
```

On client node:

```bash
export KEYSTREL_SERVER="tcp://<tailscale-ip>:8765"
export KEYSTREL_SERVER_TOKEN="<shared-secret>"
keystrel-client --verbose --no-start-chime
```

Expected:

- client connects successfully and returns a transcript response
- no local daemon socket is required on the client node

## 4) Security/Failure Checks

Run these checks whenever auth/transport code changes:

- invalid token request is rejected with `unauthorized` error
- missing token request is rejected with `unauthorized` error
- oversize request line is rejected with `request too large`
- oversize decoded audio payload is rejected with size-limit error

These checks are covered by unit tests and should stay green before release.

## 5) Release Readiness Checklist

- unit tests pass
- Python syntax checks pass
- local Unix-socket smoke test passes
- remote Tailnet smoke test passes
- docs updated (`README.md`, `docs/CHEATSHEET.md`, `docs/TESTING.md`, `docs/AGENTS.md` as needed)
- no real secrets committed (`KEYSTREL_SERVER_TOKEN` must remain placeholder in repo files)

## 6) Desktop/Audio Manual Test Strategies

Some behavior depends on live audio devices, desktop session state, and human interaction.
Use this checklist for release validation on a real workstation.

PTT typing and focus routing (X11):

```bash
echo "$XDG_SESSION_TYPE"
keystrel-ptt
```

Verify transcript lands in the actively focused text field and lock/debounce prevent duplicate runs.

Chime backend audibility and routing:

```bash
keystrel-client --verbose --max-seconds 0.5 --chime-backend pipewire
keystrel-client --verbose --max-seconds 0.5 --chime-backend paplay
keystrel-client --verbose --max-seconds 0.5 --chime-backend canberra
```

Mute/restore behavior across sinks:

```bash
pactl list short sinks
keystrel-client --verbose --max-seconds 1.5
pactl list short sinks
```

Confirm sink mute states are restored to original values.

Microphone/VAD quality pass:

```bash
keystrel-client --verbose --webrtcvad-mode 2 --speech-ratio 0.60 --start-speech-chunks 2
keystrel-client --verbose --webrtcvad-mode 3 --speech-ratio 0.72 --start-speech-chunks 3
```

Validate false-trigger resistance in background noise and sentence pickup for normal speech.
