# claude-call (Linux + safety fork) 📞🐧

![license: MIT](https://img.shields.io/badge/license-MIT-green) ![python 3.12](https://img.shields.io/badge/python-3.12-blue) ![os: Linux](https://img.shields.io/badge/os-Linux-orange)

Fork of [caiovicentino/claude-call](https://github.com/caiovicentino/claude-call) — *"Talk to your Claude Code by voice — a real phone call with your terminal agent."* All credit for the original design (local STT/TTS pipeline, session-resuming daemon, hook mode) goes to the upstream author. This fork exists because three things were missing that mattered for running it unattended on Linux — see [`PR #3`](https://github.com/caiovicentino/claude-call/pull/3) for the same changes proposed upstream.

## What this fork adds

- **Linux audio cues.** Upstream's `sounds.py` only supports macOS (`afplay`) and Windows (`winsound`) — on Linux, every wake/heard/mute sound cue was a silent no-op. This fork decodes the freedesktop sound theme (`.oga`, present on most distros) with `ffmpeg` (already a dependency) piped into `aplay`. No new dependencies, no config needed.
- **A hard anti-runaway safety net.** Without headphones, the mic has to be half-duplex-muted while the assistant talks (there's no other way to stop it hearing its own voice) — which means **you can't interrupt it by voice**. Two guards fix the failure modes that causes:
  - `CALL_MAX_PER_HOUR` (default `20`) — hard rate-limit on brain invocations. A false wake-word trigger from background noise can otherwise loop unattended and burn real API credit.
  - `CALL_MAX_SPOKEN_CHARS` (default `260`) — hard-truncates a turn's spoken reply *and cancels the underlying generation* once hit, instead of letting the model read out a full markdown list because it ignored the "keep it short" instruction.
- **A real Italian voice pack.** Upstream only had `en`/`pt` voice-call system prompts; `CALL_LANG=it` silently fell back to English. Added a proper Italian one (and fixed a hardcoded-Portuguese reminder string in hook mode that ignored `CALL_LANG` entirely).
- **A systemd `--user` service** ([`systemd/claude-call.service`](./systemd/claude-call.service)) so assistant mode (wake-word, always listening) survives logout/reboot instead of dying with your terminal. Confirmed this matters: launching it from an SSH shell or a backgrounded `nohup` silently breaks mic access (`XDG_RUNTIME_DIR` isn't set outside a real user session, so PipeWire never connects) — a real systemd user service gets that environment for free.

Everything else — the architecture, the install script, the session-resuming daemon, the cost model — is upstream's. Read **their** README for that: https://github.com/caiovicentino/claude-call

## Always-on (systemd)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/claude-call.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-call.service
systemctl --user status claude-call.service   # confirm it's actually listening
```

## Picking a wake word (if your language isn't English)

Whisper transcribes a foreign name inconsistently when spoken in a non-English sentence —
in testing, `CALL_NAME=Claude` came back as "Cloud", "Proud", or garbage about half the
time when spoken in Italian, and `Jarvis` came back as "Giarvis" (the Italian "gi" for
the English "j" sound) often enough to make the fuzzy wake-word matcher miss it. Neither
is a config bug — whisper-small just isn't reliable on transliterated foreign names.

**Pick a wake word that's spelled and pronounced the same in your language**, e.g.
`CALL_NAME=Computer` — worked reliably in Italian in testing, no alias workarounds needed.
If you're stuck with a name that keeps getting mistranscribed, `CALL_WAKE=name1,name2,...`
takes a comma-separated list of accepted mishearings as a stopgap, but changing the word
itself is the real fix.

## Install

Same installer as upstream, pointed at this fork:

```bash
git clone https://github.com/davixspain/claude-call-linux
cd claude-call-linux
./install.sh
```

## New env vars

| Var | Default | What it does |
|---|---|---|
| `CALL_MAX_PER_HOUR` | `20` | Hard cap on brain (`claude -p`) invocations per rolling hour. |
| `CALL_MAX_SPOKEN_CHARS` | `260` | Hard cap on characters spoken per turn; cuts generation short once hit. |
| `CALL_LANG=it` | — | Now has a real Italian voice-call system prompt instead of falling back to English. |

## Why fork instead of just patch

The PR is open upstream ([#3](https://github.com/caiovicentino/claude-call/pull/3)) — if it merges, use the original repo. This fork exists so the Linux fix and the safety net are usable today without waiting on a merge, and so anyone hitting the same "silent on Linux" or "burned my agent credit to a false wake-word loop overnight" problem can find a fix immediately.

## License

MIT, same as upstream — original copyright notice preserved in [`LICENSE`](./LICENSE).
